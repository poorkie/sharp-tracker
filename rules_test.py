"""Test candidate pick rules on games BEFORE live picks began.

Wallets are graded on games before --cutoff. Then for every test-period game
(--cutoff to --end) where 2+ Elite wallets held positions, Elite trades are
replayed in time order up to 15 minutes before start. The first moment each
rule is satisfied is its "alert"; it is graded at the lock price (what a
follower could get), with CLV = lock price - alert price.

    python rules_test.py --cutoff 2026-09-01 --end 2026-11-10
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from backtest import fetch_history, lock_price, to_bet
from sharp import api, grading
from sharp.markets import parse_time

ROOT = Path(__file__).parent
log = logging.getLogger("rules")

# name -> (min elite wallets, min elite $, min hours before start at alert)
RULES = {
    "A  live rule: 2+ Elite, $2k":            (2, 2000, 0),
    "B  3+ Elite, $2k":                       (3, 2000, 0),
    "C  2+ Elite, $6k":                       (2, 6000, 0),
    "D  3+ Elite, $6k":                       (3, 6000, 0),
    "E  2+ Elite, $2k, alert 2h+ early":      (2, 2000, 2),
    "F  3+ Elite, $6k, alert 2h+ early":      (3, 6000, 2),
    "S  single Elite $5k+ (live rule)":       (1, 5000, 0),
}
OPP_RATIO = 0.35


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-09-01")
    ap.add_argument("--end", default="2026-11-10")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    sports, gcfg = cfg["sports"], cfg["grading"]

    wallets = list(json.loads((ROOT / "state/wallets.json").read_text()))
    with ThreadPoolExecutor(args.workers) as ex:
        histories = list(ex.map(fetch_history, wallets))

    train, test = defaultdict(list), defaultdict(list)
    for rows in histories:
        for r in rows:
            x = to_bet(r, sports)
            if not x:
                continue
            d, bet, row = x
            if d < args.cutoff:
                train[row["_wallet"]].append(bet)
            elif d < args.end:
                test[row["_wallet"]].append((bet, row))
    tier = {}
    for w, bets in train.items():
        for s in {b.sport for b in bets}:
            tier[(w, s)] = grading.grade_bets(bets, s, gcfg).tier

    holders = defaultdict(set)
    sport_of = {}
    for w, items in test.items():
        for b, r in items:
            if tier.get((w, b.sport)) == "ELITE":
                holders[r["condition_id"]].add(w)
                sport_of[r["condition_id"]] = b.sport
    cids = [c for c, ws in holders.items() if len(ws) >= 1]
    log.info("%d test markets with Elite positions", len(cids))
    markets = {m.get("conditionId"): m for m in api.markets_by_condition(cids, closed=True)}

    def one(cid):
        m = markets.get(cid)
        if not m:
            return []
        start = parse_time(m.get("gameStartTime")) or parse_time(m.get("eventStartTime"))
        toks = api.parse_json_list(m.get("clobTokenIds"))
        prices = [float(p) for p in api.parse_json_list(m.get("outcomePrices"))]
        if not start or not toks or len(toks) != len(prices):
            return []
        if any(0.01 < p < 0.99 for p in prices):
            return []  # void / unresolved
        lock_ts = int(start.timestamp()) - 15 * 60
        try:
            trades = api.trades_for_conditions([cid], 200, lock_ts - 7 * 86400, max_rows=60000)
        except Exception as e:
            log.warning("trades %s: %s", cid, e)
            return []
        trades = sorted((t for t in trades if int(t.get("timestamp", 0)) <= lock_ts),
                        key=lambda t: int(t["timestamp"]))
        sport = sport_of[cid]
        net = defaultdict(lambda: defaultdict(float))     # idx -> wallet -> net $
        fired = {}
        for t in trades:
            w = (t.get("proxy_wallet") or "").lower()
            tr = tier.get((w, sport))
            if tr not in ("SHARP", "ELITE"):
                continue
            idx = int(t.get("outcome_index", -1))
            if idx < 0 or idx >= len(toks):
                continue
            cash = float(t["size"]) * float(t["price"])
            net[idx][w] += cash if t.get("side") == "BUY" else -cash
            if tr != "ELITE" or t.get("side") != "BUY":
                continue
            elites = {x: v for x, v in net[idx].items() if v > 0 and tier.get((x, sport)) == "ELITE"}
            total = sum(elites.values())
            opp = sum(v for j, o in net.items() if j != idx for v in o.values() if v > 0)
            hours = (lock_ts + 900 - int(t["timestamp"])) / 3600
            for name, (n_min, usd_min, h_min) in RULES.items():
                if name in fired or hours < h_min:
                    continue
                if n_min == 1:
                    ok = any(v >= usd_min for v in elites.values())
                else:
                    ok = len(elites) >= n_min and total >= usd_min
                if ok and opp <= OPP_RATIO * total:
                    fired[name] = {"idx": idx, "alert_price": float(t["price"]), "hours": hours}
        out = []
        lp_cache = {}
        for name, f in fired.items():
            idx = f["idx"]
            if idx not in lp_cache:
                lp_cache[idx] = lock_price(toks[idx], start)
            lp = lp_cache[idx]
            out.append({"rule": name, "cid": cid, "sport": sport,
                        "type": m.get("sportsMarketType") or "moneyline",
                        "won": prices[idx] >= 0.99, "alert_price": f["alert_price"],
                        "lock_price": lp, "hours": round(f["hours"], 1)})
        return out

    with ThreadPoolExecutor(args.workers) as ex:
        sigs = [s for batch in ex.map(one, cids) for s in batch]
    log.info("%d rule firings", len(sigs))

    def stats(rows):
        rs = [r for r in rows if r["lock_price"] and 0.05 <= r["lock_price"] <= 0.95]
        n = len(rs)
        if not n:
            return None
        w = sum(r["won"] for r in rs)
        pnl = sum(100 * (1 - r["lock_price"]) / r["lock_price"] if r["won"] else -100 for r in rs)
        exp = sum(r["lock_price"] for r in rs)
        sd = math.sqrt(sum(r["lock_price"] * (1 - r["lock_price"]) for r in rs)) or 1
        clv = sum(r["lock_price"] - r["alert_price"] for r in rs) / n
        return {"n": n, "w": w, "units": round(pnl / 100, 2), "roi": pnl / (100 * n),
                "z": (w - exp) / sd, "clv": clv}

    def line(label, s):
        if not s:
            return f"| {label} | 0 | | | | | |"
        return (f"| {label} | {s['n']} | {s['w']}-{s['n'] - s['w']} | {s['units']:+.2f} | {s['roi']:+.1%} "
                f"| {s['clv'] * 100:+.1f}¢ | {s['z']:+.2f} |")

    head = "| Rule | Bets | Record | Units | ROI @ lock | CLV | Luck-adjusted z |\n|---|---|---|---|---|---|---|"
    L = [f"# Rule test: graded before {args.cutoff}, tested {args.cutoff} to {args.end} (before live picks)", "",
         "Graded at the price 15 min before start. z = wins above what the prices implied, in standard "
         "deviations (about +2 or more is strong evidence; between -1 and +1 is indistinguishable from no edge).", "",
         "## All bet types", head]
    for name in RULES:
        L.append(line(name, stats([s for s in sigs if s["rule"] == name])))
    for t in ("moneyline", "spreads", "totals"):
        L += ["", f"## {t}", head]
        for name in RULES:
            L.append(line(name, stats([s for s in sigs if s["rule"] == name and s["type"] == t])))
    L += ["", "## Live rule A by sport", head]
    for sp in sorted({s["sport"] for s in sigs}):
        L.append(line(sp, stats([s for s in sigs if s["rule"].startswith("A") and s["sport"] == sp])))
    (ROOT / "RULES.md").write_text("\n".join(L) + "\n")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs/rules_test.json").write_text(json.dumps(sigs, indent=1))
    print("\n".join(L))


if __name__ == "__main__":
    main()
