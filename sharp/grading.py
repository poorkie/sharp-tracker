"""Wallet grading: decide who is actually sharp, per sport.

Why this beats "big wallet = smart money":
  * Uses every resolved bet, INCLUDING losing tickets that were never
    redeemed. Losers are worth $0 so many wallets never redeem them;
    tools that only read "closed" positions miss them and make
    everyone look better than they are.
  * Scores per sport. A great NBA bettor gets no credit on NHL.
  * Shrinks small samples toward a losing prior, and requires a
    t-stat on per-bet returns, so a hot streak isn't enough.
  * Reports a price-adjusted hit rate: winning 55% while buying at
    an average of 45 cents is skill; winning 55% at 70 cents isn't.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

DATE_IN_SLUG = re.compile(r"\d{4}-\d{2}-\d{2}")


def sport_of(event_slug: str, sports: dict) -> str | None:
    prefix = (event_slug or "").split("-", 1)[0].lower()
    return prefix if prefix in sports else None


@dataclass
class Bet:
    sport: str
    cost: float
    pnl: float
    entry_price: float
    won: bool


def bets_from_positions(rows: list[dict], sports: dict, games_only: bool) -> list[Bet]:
    """Turn v2 position rows (CLOSED + REDEEMABLE) into resolved bets."""
    seen: set[tuple] = set()
    bets: list[Bet] = []
    for r in rows:
        key = (r.get("condition_id"), r.get("token_id"))
        if key in seen:
            continue
        seen.add(key)
        slug = r.get("event_slug") or r.get("slug") or ""
        sport = sport_of(slug, sports)
        if not sport:
            continue
        if games_only and not DATE_IN_SLUG.search(slug):
            continue
        avg = float(r.get("avg_price") or 0)
        size = float(r.get("total_size") or 0)
        cost = size * avg or float(r.get("total_cost_usdc") or 0)
        if cost < 1 or not (0 < avg < 1):
            continue
        status = (r.get("status") or "").upper()
        if status == "CLOSED":
            pnl = float(r.get("realized_pnl") or 0)
        else:  # resolved but still held: winners worth $1, losers $0
            pnl = float(r.get("total_pnl") or 0)
        bets.append(Bet(sport, cost, pnl, avg, pnl > 0))
    return bets


@dataclass
class Grade:
    sport: str
    n: int
    staked: float
    pnl: float
    roi: float          # raw dollar-weighted ROI
    shrunk_roi: float   # ROI after small-sample shrinkage
    t_stat: float       # t-stat of per-bet returns
    hit_rate: float
    avg_price: float
    hit_edge: float     # hit_rate - avg_price
    tier: str           # ELITE / SHARP / NONE

    def to_dict(self) -> dict:
        return asdict(self)


def grade_bets(bets: list[Bet], sport: str, cfg: dict) -> Grade:
    b = [x for x in bets if x.sport == sport]
    n = len(b)
    staked = sum(x.cost for x in b)
    pnl = sum(x.pnl for x in b)
    roi = pnl / staked if staked else 0.0
    avg_cost = staked / n if n else 0.0
    k = cfg["prior_bets"]
    shrunk = ((pnl + cfg["prior_roi"] * k * avg_cost) / (staked + k * avg_cost)
              if n else cfg["prior_roi"])
    rets = [x.pnl / x.cost for x in b]
    if n >= 2:
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)
        t = mean / math.sqrt(var / n) if var > 0 else 0.0
    else:
        t = 0.0
    hit = sum(x.won for x in b) / n if n else 0.0
    avg_p = sum(x.entry_price for x in b) / n if n else 0.0
    tier = "NONE"
    if n >= cfg["min_bets"]:
        if shrunk >= cfg["elite"]["min_roi"] and t >= cfg["elite"]["min_t"]:
            tier = "ELITE"
        elif shrunk >= cfg["sharp"]["min_roi"] and t >= cfg["sharp"]["min_t"]:
            tier = "SHARP"
    return Grade(sport, n, round(staked, 2), round(pnl, 2), round(roi, 4),
                 round(shrunk, 4), round(t, 2), round(hit, 3), round(avg_p, 3),
                 round(hit - avg_p, 3), tier)


def grade_wallet(rows: list[dict], sports: dict, cfg: dict) -> dict[str, dict]:
    bets = bets_from_positions(rows, sports, cfg.get("games_only", True))
    return {s: grade_bets(bets, s, cfg).to_dict()
            for s in sorted({x.sport for x in bets})}
