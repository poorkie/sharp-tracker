"""Which leagues are worth adding? For every league prefix found in wallet
histories (nba-, ucl-, cs2-, ...), grade on games before the cutoff and
measure results after it, plus market size.

    python sports_check.py --cutoff 2026-09-01
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from backtest import fetch_history
from sharp import api, grading

ROOT = Path(__file__).parent
log = logging.getLogger("sports")
DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-09-01")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    gcfg = cfg["grading"]
    current = set(cfg["sports"])

    # wallet pool: everyone already graded + extra leaderboards so non-US-sports
    # specialists (esports, soccer) are represented
    pool = set(json.loads((ROOT / "state/wallets.json").read_text()))
    for cat in ("sports", "esports", "soccer"):
        for period in ("month", "all"):
            try:
                rows = api.leaderboard(cat, period, 500)
                pool |= {(r.get("user_id") or "").lower() for r in rows if r.get("user_id")}
                log.info("leaderboard %s/%s: %d", cat, period, len(rows))
            except Exception as e:
                log.info("leaderboard %s/%s unavailable: %s", cat, period, e)
    pool.discard("")
    log.info("wallet pool: %d", len(pool))
    with ThreadPoolExecutor(args.workers) as ex:
        histories = list(ex.map(fetch_history, sorted(pool)))

    # every game-like prefix becomes a "sport"
    prefixes = set()
    for rows in histories:
        for r in rows:
            slug = r.get("event_slug") or ""
            if DATE.search(slug):
                prefixes.add(slug.split("-", 1)[0].lower())
    sports = {p: {} for p in prefixes}

    train, test = defaultdict(list), defaultdict(list)
    test_cids = defaultdict(set)
    for rows in histories:
        for r in rows:
            slug = r.get("event_slug") or ""
            m = DATE.search(slug)
            if not m:
                continue
            bets = grading.bets_from_positions([r], sports, True)
            if not bets:
                continue
            b = bets[0]
            if m.group(1) < args.cutoff:
                train[r["_wallet"]].append(b)
            else:
                test[r["_wallet"]].append(b)
                test_cids[b.sport].add(r.get("condition_id"))

    tier = {}
    for w, bets in train.items():
        for s in {b.sport for b in bets}:
            tier[(w, s)] = grading.grade_bets(bets, s, gcfg).tier

    per = defaultdict(lambda: defaultdict(list))   # sport -> tier -> bets
    for w, bets in test.items():
        for b in bets:
            per[b.sport][tier.get((w, b.sport), "UNGRADED")].append(b)

    def roi(bets):
        st = sum(b.cost for b in bets)
        return (sum(b.pnl for b in bets) / st) if st else None

    rows = []
    for sport, groups in per.items():
        test_usd = sum(b.cost for g in groups.values() for b in g)
        if test_usd < 50_000:
            continue
        elites = {w for (w, s), t in tier.items() if s == sport and t == "ELITE"}
        e = groups.get("ELITE", [])
        rest = [b for t, g in groups.items() if t != "ELITE" for b in g]
        rows.append({"sport": sport, "current": sport in current,
                     "elite_wallets": len(elites), "elite_bets": len(e),
                     "elite_usd": round(sum(b.cost for b in e)),
                     "elite_roi": roi(e), "rest_roi": roi(rest),
                     "test_usd": round(test_usd), "markets": len(test_cids[sport])})
    rows.sort(key=lambda r: -r["test_usd"])
    rows = rows[:45]

    # market size: median volume of a sample of markets per league
    for r in rows:
        cids = list(test_cids[r["sport"]])[:60]
        vols = []
        try:
            for m in api.markets_by_condition(cids, closed=True):
                try:
                    vols.append(float(m.get("volumeNum") or m.get("volume")))
                except (TypeError, ValueError):
                    pass
        except Exception as ex:
            log.warning("volume %s: %s", r["sport"], ex)
        r["median_market_volume"] = round(statistics.median(vols)) if vols else None

    def verdict(r):
        if r["elite_bets"] < 40:
            return "too few Elite bets"
        if (r["median_market_volume"] or 0) < 20_000:
            return "markets too small"
        if r["elite_roi"] is not None and r["elite_roi"] > 0.03 and \
                (r["rest_roi"] is None or r["elite_roi"] > r["rest_roi"] + 0.02):
            return "ADD"
        return "Elite not beating the rest"

    for r in rows:
        r["verdict"] = verdict(r)
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs/sports_check.json").write_text(json.dumps(rows, indent=1))
    L = [f"# League check (graded before {args.cutoff}, results after)", "",
         f"Wallet pool: {len(pool)}", "",
         "| League | Now? | Elite wallets | Elite bets | Elite $ | Elite ROI | Everyone else ROI | All $ after cutoff | Markets | Median market volume | Verdict |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        f = lambda v: "–" if v is None else f"{v:+.1%}"
        L.append(f"| {r['sport']} | {'yes' if r['current'] else ''} | {r['elite_wallets']} | {r['elite_bets']} | "
                 f"${r['elite_usd']:,} | {f(r['elite_roi'])} | {f(r['rest_roi'])} | ${r['test_usd']:,} | "
                 f"{r['markets']} | ${(r['median_market_volume'] or 0):,} | {r['verdict']} |")
    (ROOT / "SPORTS.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
