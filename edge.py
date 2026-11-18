"""Where does the Elite edge come from?

Wallets are graded on games before --cutoff. For test-period games (--cutoff
to --end) with Elite positions, every Elite BUY is bucketed by when it was
placed relative to the game start (days early ... in-game), and we measure:

  * ROI at the price THEY paid            (is the wallet profitable here?)
  * CLV to the pre-game close              (did the market move their way?)
  * ROI if you copied at the pre-game close (what a late follower gets)

Same numbers for non-Elite wallets as a baseline.

    python edge.py --cutoff 2026-09-01 --end 2026-11-10
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from backtest import fetch_history, lock_price, to_bet
from sharp import api, grading
from sharp.markets import parse_time

ROOT = Path(__file__).parent
log = logging.getLogger("edge")
BUCKETS = [(48, 1e9, "48h+ before"), (24, 48, "24-48h before"), (6, 24, "6-24h before"),
           (1, 6, "1-6h before"), (0.25, 1, "15-60 min before"), (0, 0.25, "last 15 min"),
           (-1e9, 0, "in-game")]


def bucket(hours: float) -> str:
    return next(name for lo, hi, name in BUCKETS if lo <= hours < hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-09-01")
    ap.add_argument("--end", default="2026-11-10")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-markets", type=int, default=1500)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    sports, gcfg = cfg["sports"], cfg["grading"]

    wallets = list(json.loads((ROOT / "state/wallets.json").read_text()))
    with ThreadPoolExecutor(args.workers) as ex:
        histories = list(ex.map(fetch_history, wallets))
    train, test_cids, sport_of = defaultdict(list), defaultdict(set), {}
    for rows in histories:
        for r in rows:
            x = to_bet(r, sports)
            if not x:
                continue
            d, bet, row = x
            if d < args.cutoff:
                train[row["_wallet"]].append(bet)
            elif d < args.end:
                test_cids[row["condition_id"]].add(row["_wallet"])
                sport_of[row["condition_id"]] = bet.sport
    tier = {}
    for w, bets in train.items():
        for s in {b.sport for b in bets}:
            tier[(w, s)] = grading.grade_bets(bets, s, gcfg).tier
    cids = [c for c, ws in test_cids.items()
            if any(tier.get((w, sport_of[c])) == "ELITE" for w in ws)][: args.max_markets]
    log.info("%d test markets with Elite positions", len(cids))
    markets = {m.get("conditionId"): m for m in api.markets_by_condition(cids, closed=True)}

    def one(cid):
        m = markets.get(cid)
        if not m:
            return []
        start = parse_time(m.get("gameStartTime")) or parse_time(m.get("eventStartTime"))
        toks = api.parse_json_list(m.get("clobTokenIds"))
        prices = [float(p) for p in api.parse_json_list(m.get("outcomePrices"))]
        if not start or not toks or len(toks) != len(prices) or any(0.01 < p < 0.99 for p in prices):
            return []
        st = int(start.timestamp())
        try:
            trades = api.trades_for_conditions([cid], 200, st - 10 * 86400, max_rows=60000)
        except Exception as e:
            log.warning("trades %s: %s", cid, e)
            return []
        sport = sport_of[cid]
        closes = {}
        out = []
        for t in trades:
            if t.get("side") != "BUY":
                continue
            w = (t.get("proxy_wallet") or "").lower()
            tr = tier.get((w, sport), "UNGRADED")
            group = "ELITE" if tr == "ELITE" else ("NONE" if tr in ("NONE", "UNGRADED") else None)
            if not group:
                continue
            idx = int(t.get("outcome_index", -1))
            if not 0 <= idx < len(toks):
                continue
            hours = (st - int(t["timestamp"])) / 3600
            pre = hours > 0.25
            if pre and idx not in closes:
                closes[idx] = lock_price(toks[idx], start)
            out.append({"g": group, "b": bucket(hours), "usd": float(t["size"]) * float(t["price"]),
                        "p": float(t["price"]), "won": prices[idx] >= 0.99,
                        "close": closes.get(idx) if pre else None, "sport": sport,
                        "type": m.get("sportsMarketType") or "moneyline"})
        return out

    with ThreadPoolExecutor(args.workers) as ex:
        buys = [b for batch in ex.map(one, cids) for b in batch]
    log.info("%d buys", len(buys))

    def stats(rows):
        rows = [r for r in rows if 0.03 <= r["p"] <= 0.97]
        usd = sum(r["usd"] for r in rows)
        if not usd:
            return None
        pnl = sum(r["usd"] * ((1 - r["p"]) / r["p"] if r["won"] else -1) for r in rows)
        c = [r for r in rows if r["close"] and 0.03 <= r["close"] <= 0.97]
        cu = sum(r["usd"] for r in c)
        clv = sum(r["usd"] * (r["close"] - r["p"]) for r in c) / cu if cu else None
        # follower: same side, same $, bought at the pre-game close instead
        fpnl = sum(r["usd"] * ((1 - r["close"]) / r["close"] if r["won"] else -1) for r in c)
        return {"buys": len(rows), "usd": usd, "roi": pnl / usd, "clv": clv,
                "follow_roi": fpnl / cu if cu else None}

    def fmt(s):
        if not s:
            return "| 0 | | | | |"
        f = lambda v, pct=True: "–" if v is None else (f"{v:+.1%}" if pct else f"{v * 100:+.1f}¢")
        return (f"| {s['buys']:,} | ${s['usd']:,.0f} | {f(s['roi'])} | {f(s['clv'], False)} "
                f"| {f(s['follow_roi'])} |")

    head = ("| When they bet | Buys | $ | ROI at their price | CLV to pre-game close | "
            "ROI if copied at close |\n|---|---|---|---|---|---|")
    L = [f"# Where does the Elite edge come from? (graded before {args.cutoff}, games {args.cutoff} to {args.end})", ""]
    for g, title in (("ELITE", "Elite wallets"), ("NONE", "Everyone else (baseline)")):
        L += [f"## {title}", head]
        for _, _, name in BUCKETS:
            L.append(f"| {name} " + fmt(stats([b for b in buys if b["g"] == g and b["b"] == name])))
        L.append("| **all** " + fmt(stats([b for b in buys if b["g"] == g])))
        L.append("")
    tot = sum(b["usd"] for b in buys if b["g"] == "ELITE") or 1
    L += ["## Share of Elite money by timing", "", "| When | Share |", "|---|---|"]
    for _, _, name in BUCKETS:
        L.append(f"| {name} | {sum(b['usd'] for b in buys if b['g'] == 'ELITE' and b['b'] == name) / tot:.1%} |")
    L += ["", "## Elite by bet type", head]
    for t in ("moneyline", "spreads", "totals"):
        L.append(f"| {t} " + fmt(stats([b for b in buys if b["g"] == "ELITE" and b["type"] == t])))
    (ROOT / "EDGE.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
