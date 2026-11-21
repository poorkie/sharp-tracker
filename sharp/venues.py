"""Prices for the same bet on US-legal exchanges: Kalshi and Polymarket US.

The Elite wallets trade on polymarket.com, which US users can't use. When an
in-game pick fires we look up, at that same moment, what the same side
costs on:
  - Polymarket US (gateway.polymarket.us): games usually share the
    polymarket.com event slug. Match winner market = "aec-<slug>", map N =
    "astatc-<slug>-gameN" (a Yes/No market on the first-listed team).
  - Kalshi (api.elections.kalshi.com): game/map winner series, matched by
    team name and game date.
Both are public, no account or keys needed. Every lookup fails safe: if the
game can't be matched with confidence we record why and move on.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

import requests

from .odds import team_matches

log = logging.getLogger(__name__)

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PMUS = "https://gateway.polymarket.us"
_s = requests.Session()
_s.headers["User-Agent"] = "sharp-tracker/1.0"

# Kalshi series per (sport, market type)
KALSHI_SERIES = {
    ("dota2", "moneyline"): "KXDOTA2GAME", ("dota2", "child_moneyline"): "KXDOTA2MAP",
    ("lol", "moneyline"): "KXLOLGAME", ("lol", "child_moneyline"): "KXLOLMAP",
    ("nba", "moneyline"): "KXNBAGAME", ("nhl", "moneyline"): "KXNHLGAME",
    ("cbb", "moneyline"): "KXNCAAMBGAME", ("atp", "moneyline"): "KXATPMATCH",
    ("wta", "moneyline"): "KXWTAMATCH",
}
KALSHI_FEE_RATE = 0.07          # taker fee ~ 0.07 x P x (1-P) per $1 contract

_cache: dict[str, tuple[float, list]] = {}


def _get(url: str, params: dict | None = None) -> dict:
    r = _s.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def map_number(m: dict) -> int | None:
    g = re.search(r"\b(game|map)\s*(\d+)", m.get("question") or "", re.I)
    return int(g.group(2)) if g else None


# ---------------- Polymarket US ----------------

def pmus_quote(m: dict, idx: int) -> dict:
    slug = m.get("event_slug") or ""
    try:
        ev = _get(f"{PMUS}/v1/events/slug/{slug}")
    except requests.HTTPError as e:
        return {"status": "not_listed" if e.response is not None and e.response.status_code == 404 else "error",
                "reason": str(e)[:120]}
    ev = ev.get("event") or ev
    mkts = ev.get("markets") or []
    pick = (m.get("outcomes") or [""] * 9)[idx]
    if m["market_type"] == "child_moneyline":
        n = map_number(m)
        mk = next((x for x in mkts if n and (x.get("slug") or "").endswith(f"-game{n}")), None)
        if not mk:
            return {"status": "no_market", "reason": f"no map {n} market"}
        q = mk.get("question") or ""
        first = re.match(r"Will (?:the )?(.+?) win Game", q)
        if not first:
            return {"status": "no_market", "reason": "can't read map market"}
        is_first = team_matches(pick, first.group(1))
        side = 0 if is_first else 1
        if not is_first and not any(team_matches(o, first.group(1)) for o in m["outcomes"] if o != pick):
            return {"status": "no_match", "reason": f"team names differ: {first.group(1)}"}
    else:
        mk = next((x for x in mkts if x.get("marketType") == "moneyline"
                   and (x.get("slug") or "").startswith("aec-")), None)
        if not mk:
            return {"status": "no_market", "reason": "no match-winner market"}
        outs = mk.get("outcomes")
        outs = json.loads(outs) if isinstance(outs, str) else (outs or [])
        side = next((i for i, o in enumerate(outs) if team_matches(pick, o)), None)
        if side is None:
            return {"status": "no_match", "reason": f"team names differ: {outs}"}
    try:
        bbo = _get(f"{PMUS}/v1/markets/{mk['slug']}/bbo").get("marketData") or {}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:120]}
    ask = float((bbo.get("bestAsk") or {}).get("value") or 0) or None
    bid = float((bbo.get("bestBid") or {}).get("value") or 0) or None
    # the book is quoted for the first outcome; the other side costs 1 - bid
    price = ask if side == 0 else (round(1 - bid, 4) if bid else None)
    rec = {"market": mk["slug"], "side": side, "raw_ask": ask, "raw_bid": bid,
           "url": f"https://polymarket.us/event/{slug}"}
    if not price or price >= 0.995:
        return {**rec, "status": "no_liquidity"}
    return {**rec, "status": "ok", "ask": round(price, 4)}


# ---------------- Kalshi ----------------

def _kalshi_open(series: str) -> list[dict]:
    hit = _cache.get(series)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    rows, cursor = [], None
    for _ in range(6):
        params = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        body = _get(f"{KALSHI}/markets", params)
        rows += body.get("markets") or []
        cursor = body.get("cursor")
        if not cursor:
            break
    _cache[series] = (time.time(), rows)
    return rows


def _kalshi_team(mk: dict) -> str:
    r = re.match(r"If (?:the )?(.+?) wins?\b", mk.get("rules_primary") or "")
    return r.group(1) if r else (mk.get("yes_sub_title") or "")


def kalshi_quote(m: dict, idx: int) -> dict:
    series = KALSHI_SERIES.get((m["sport"], m["market_type"]))
    if not series:
        return {"status": "not_offered", "reason": "Kalshi has no matching market type"}
    outs = m.get("outcomes") or []
    if len(outs) != 2:
        return {"status": "not_offered", "reason": "not a two-sided market"}
    pick, other = outs[idx], outs[1 - idx]
    try:
        start = datetime.fromisoformat(m["game_start"]).timestamp()
        mkts = _kalshi_open(series)
    except Exception as e:
        return {"status": "error", "reason": str(e)[:120]}
    n = map_number(m) if m["market_type"] == "child_moneyline" else None
    by_event: dict[str, list] = {}
    for mk in mkts:
        by_event.setdefault(mk.get("event_ticker", ""), []).append(mk)
    for ev, group in by_event.items():
        if n and not ev.endswith(f"-{n}"):
            continue
        a = next((x for x in group if team_matches(pick, _kalshi_team(x))), None)
        b = next((x for x in group if x is not a and team_matches(other, _kalshi_team(x))), None)
        if not a or not b:
            continue
        when = a.get("occurrence_datetime") or a.get("expected_expiration_time")
        try:
            t = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
        except Exception:
            t = start
        if abs(t - start) > 36 * 3600:
            continue
        try:
            fresh = _get(f"{KALSHI}/markets/{a['ticker']}").get("market") or a
        except Exception:
            fresh = a
        ask = float(fresh.get("yes_ask_dollars") or 0)
        rec = {"ticker": a["ticker"], "team": _kalshi_team(a),
               "ask_size": float(fresh.get("yes_ask_size_fp") or 0),
               "url": f"https://kalshi.com/markets/{series.lower()}"}
        if not ask or ask >= 0.995:
            return {**rec, "status": "no_liquidity"}
        return {**rec, "status": "ok", "ask": round(ask, 4),
                "fee": round(KALSHI_FEE_RATE * ask * (1 - ask), 4)}
    return {"status": "no_match", "reason": "game not found open on Kalshi"}


def quotes(m: dict, idx: int) -> dict:
    """Both venues; never raises."""
    out = {}
    for name, fn in (("kalshi", kalshi_quote), ("pmus", pmus_quote)):
        try:
            out[name] = fn(m, idx)
        except Exception as e:
            out[name] = {"status": "error", "reason": str(e)[:120]}
    return out


def pnl_100(price: float, result: str, fee: float = 0.0) -> float | None:
    """$100 stake at `price` (+ fee per $1 contract)."""
    cost = price + fee
    if result == "WIN":
        return round(100 * (1 - cost) / cost, 2)
    if result == "LOSS":
        return -100.0
    return 0.0
