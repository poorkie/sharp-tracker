"""Do Elite wallets bet props, and do they win on them?

Grades wallets on games before the cutoff (same as backtest.py), then on
games after the cutoff breaks results down by market class:
moneyline / spread / total / player prop / team prop / other.

    python props.py --cutoff 2026-09-01
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from backtest import fetch_history, group_stats, to_bet
from sharp import api, grading

ROOT = Path(__file__).parent
log = logging.getLogger("props")

PLAYER_STATS = re.compile(
    r"\b(points|pts|assists|rebounds|rebs|threes|3-pointers|steals|blocks|pra|"
    r"passing|rushing|receiving|receptions|yards|touchdowns?|tds?|interceptions|"
    r"strikeouts|hits|home runs?|rbis?|total bases|walks|saves|shots on goal|sog|"
    r"anytime|first goal|goalscorer|to score|double[- ]double)\b", re.I)
TEAM_PROP = re.compile(r"\b(first (half|5|five|quarter|period|inning)|1h|1q|f5|"
                       r"team total|both teams|btts|first to|race to|overtime|exact)\b", re.I)


def classify(row: dict) -> str:
    title = (row.get("title") or row.get("question") or "")
    slug = (row.get("slug") or "").lower()
    ev = (row.get("event_slug") or "").lower()
    t = title.lower()
    if PLAYER_STATS.search(t) and not re.search(r"\bvs\.?\b", t.split(":")[0]):
        return "player_prop"
    if PLAYER_STATS.search(slug.replace("-", " ")):
        return "player_prop"
    if TEAM_PROP.search(t) or TEAM_PROP.search(slug.replace("-", " ")):
        return "team_prop"
    if "spread" in t or "spread" in slug or re.search(r"\([+-]\d+(\.\d)?\)", t):
        return "spread"
    if "o/u" in t or "total" in slug or " over " in f" {t} " or "under" in t:
        return "total"
    if slug and ev and slug == ev:
        return "moneyline"
    if re.search(r"\bvs\.?\b", t) or t.startswith("will ") and " win" in t:
        return "moneyline"
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-09-01")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    sports, gcfg = cfg["sports"], cfg["grading"]

    wallets = list(json.loads((ROOT / "state/wallets.json").read_text()))
    with ThreadPoolExecutor(args.workers) as ex:
        histories = list(ex.map(fetch_history, wallets))
    sample_row = next((r for h in histories for r in h), {})
    log.info("position row fields: %s", sorted(sample_row))

    train, test = defaultdict(list), defaultdict(list)
    for rows in histories:
        for r in rows:
            x = to_bet(r, sports)
            if x:
                d, bet, row = x
                (train if d < args.cutoff else test)[row["_wallet"]].append((bet, row))

    tier = {}
    for w, items in train.items():
        bets = [b for b, _ in items]
        for s in {b.sport for b in bets}:
            tier[(w, s)] = grading.grade_bets(bets, s, gcfg).tier

    # results after the cutoff, by tier x market class
    groups = defaultdict(list)
    elite_sizes = defaultdict(list)
    elite_prop_markets = Counter()
    examples = defaultdict(list)
    for w, items in test.items():
        for b, r in items:
            cls = classify(r)
            t = tier.get((w, b.sport), "UNGRADED")
            groups[(t, cls)].append(b)
            groups[(t, "ALL")].append(b)
            if len(examples[cls]) < 8:
                examples[cls].append(r.get("title") or r.get("slug"))
            if t == "ELITE":
                elite_sizes[cls].append(b.cost)
                if cls in ("player_prop", "team_prop"):
                    elite_prop_markets[r["condition_id"]] += 1

    # liquidity of prop markets Elite wallets bet on
    cids = [c for c, _ in elite_prop_markets.most_common(400)]
    vols = []
    for m in api.markets_by_condition(cids, closed=True):
        v = m.get("volumeNum") or m.get("volume")
        try:
            vols.append(float(v))
        except (TypeError, ValueError):
            pass

    classes = ["moneyline", "spread", "total", "player_prop", "team_prop", "other", "ALL"]
    table = {f"{t}:{c}": group_stats(groups[(t, c)])
             for t in ("ELITE", "SHARP", "NONE", "UNGRADED") for c in classes if groups[(t, c)]}
    elite_total = sum(sum(v) for v in elite_sizes.values()) or 1
    share = {c: round(sum(v) / elite_total, 3) for c, v in elite_sizes.items()}
    out = {"cutoff": args.cutoff, "table": table, "elite_money_share": share,
           "elite_median_bet": {c: round(statistics.median(v)) for c, v in elite_sizes.items() if v},
           "prop_market_volume": {
               "markets": len(vols),
               "median": round(statistics.median(vols)) if vols else None,
               "p25": round(statistics.quantiles(vols, n=4)[0]) if len(vols) > 3 else None,
               "p75": round(statistics.quantiles(vols, n=4)[2]) if len(vols) > 3 else None},
           "examples": examples, "row_fields": sorted(sample_row)}
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs/props.json").write_text(json.dumps(out, indent=1, default=str))

    L = [f"# Props analysis (graded before {args.cutoff}, results after)", "",
         "## Results after the cutoff, by wallet tier and market type", "",
         "| Tier : market | Bets | Staked | P&L | ROI ($) | ROI (flat) | Hit rate | Avg price |",
         "|---|---|---|---|---|---|---|---|"]
    for k, s in table.items():
        L.append(f"| {k} | {s['bets']} | ${s['staked']:,} | ${s['pnl']:,} | {s['roi_dollar']:+.1%} "
                 f"| {s['roi_flat']:+.1%} | {s['hit_rate']:.1%} | {s['avg_price']:.3f} |")
    L += ["", "## Where Elite money goes (share of dollars, median bet)", "",
          "| Market | Share | Median bet |", "|---|---|---|"]
    for c in sorted(share, key=lambda c: -share[c]):
        L.append(f"| {c} | {share[c]:.1%} | ${out['elite_median_bet'].get(c, 0):,} |")
    pv = out["prop_market_volume"]
    L += ["", "## Liquidity of prop markets Elite wallets bet", "",
          f"{pv['markets']} markets · median total volume ${pv['median'] or 0:,} "
          f"(middle half: ${pv['p25'] or 0:,} to ${pv['p75'] or 0:,})", "",
          "## Classifier examples (sanity check)", ""]
    for c, ex in examples.items():
        L.append(f"**{c}:** " + " · ".join(str(e) for e in ex[:6]))
        L.append("")
    (ROOT / "PROPS.md").write_text("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    main()
