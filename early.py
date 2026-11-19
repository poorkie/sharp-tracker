"""Do Elite bets placed far ahead of the game hold up, and can you copy them?

Wallets are graded on games before --cutoff. For test games (--cutoff to
--end) every Elite pre-game BUY is a signal (fills on the same side within
6 hours merged; hedges skipped), bucketed by how far ahead it was placed.
A follower copies each with a flat $100 at the next real trade on that side
1 hour later, 6 hours later, or at the last pre-game trade (15+ min before
start).

    python early.py --cutoff 2026-08-01 --end 2026-11-16
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from backtest import fetch_history, to_bet
from sharp import api, grading
from sharp.markets import parse_time

ROOT = Path(__file__).parent
log = logging.getLogger("early")
WINDOWS = [(168, 1e9, "7+ days early"), (72, 168, "3-7 days early"), (48, 72, "2-3 days early"),
           (24, 48, "1-2 days early"), (6, 24, "6-24h early")]
COPIES = [("their price", None), ("1 hour later", 3600), ("6 hours later", 6 * 3600),
          ("pre-game close", "close")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-08-01")
    ap.add_argument("--end", default="2026-11-16")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-markets", type=int, default=2500)
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
    log.info("%d Elite wallet-sports, %d markets",
             sum(t == "ELITE" for t in tier.values()), len(cids))
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
            trades = api.trades_for_conditions([cid], 100, st - 21 * 86400, max_rows=80000)
        except Exception as e:
            log.warning("trades %s: %s", cid, e)
            return []
        trades = sorted((t for t in trades if int(t["timestamp"]) < st - 900),
                        key=lambda t: int(t["timestamp"]))
        if not trades:
            return []
        sport = sport_of[cid]
        tape = defaultdict(list)
        for t in trades:
            i = int(t.get("outcome_index", -1))
            if 0 <= i < len(toks):
                tape[i].append((int(t["timestamp"]), float(t["price"])))
        tts = {i: [x[0] for x in v] for i, v in tape.items()}
        held = defaultdict(lambda: defaultdict(float))
        groups, done = {}, []
        for t in trades:
            w = (t.get("proxy_wallet") or "").lower()
            idx = int(t.get("outcome_index", -1))
            if not 0 <= idx < len(toks):
                continue
            size, price, ts = float(t["size"]), float(t["price"]), int(t["timestamp"])
            if t.get("side") != "BUY":
                held[w][idx] = max(0.0, held[w][idx] - size)
                continue
            mine = held[w][idx]
            hedge = any(s > mine + 1 for j, s in list(held[w].items()) if j != idx)
            held[w][idx] += size
            if tier.get((w, sport)) != "ELITE" or hedge or size * price < 200:
                continue
            g = groups.get((w, idx))
            if g and ts - g["ts_last"] <= 6 * 3600:
                g["usd"] += size * price
                g["ts_last"] = ts
            else:
                if g:
                    done.append(g)          # a separate, later bet starts a new signal
                groups[(w, idx)] = {"ts": ts, "ts_last": ts, "p": price, "usd": size * price, "idx": idx}
        out = []
        for g in done + list(groups.values()):
            hours = (st - g["ts"]) / 3600
            win = next((name for lo, hi, name in WINDOWS if lo <= hours < hi), None)
            if not win:
                continue
            idx = g["idx"]
            row = {"window": win, "sport": sport, "won": prices[idx] >= 0.99,
                   "type": m.get("sportsMarketType") or "moneyline", "day": g["ts"] // 86400,
                   "their price": g["p"], "usd": g["usd"]}
            for label, d in COPIES[1:]:
                lst = tts.get(idx, [])
                if d == "close":
                    row[label] = tape[idx][-1][1] if lst and lst[-1] > g["ts_last"] else None
                else:
                    k = bisect.bisect_left(lst, g["ts_last"] + d)
                    row[label] = tape[idx][k][1] if k < len(lst) and lst[k] <= g["ts_last"] + d + 3 * 3600 else None
            out.append(row)
        return out

    with ThreadPoolExecutor(args.workers) as ex:
        rows = [r for batch in ex.map(one, cids) for r in batch]
    log.info("%d early Elite signals", len(rows))

    def stats(rs, key):
        rs = [r for r in rs if r.get(key) is not None and 0.03 <= r[key] <= 0.97]
        n = len(rs)
        if not n:
            return None
        w = sum(r["won"] for r in rs)
        pnl = sum(100 * (1 - r[key]) / r[key] if r["won"] else -100 for r in rs)
        exp = sum(r[key] for r in rs)
        sd = math.sqrt(sum(r[key] * (1 - r[key]) for r in rs)) or 1
        return {"n": n, "w": w, "units": pnl / 100, "roi": pnl / (100 * n), "z": (w - exp) / sd}

    def cell(s):
        return f"{s['n']} · {s['roi']:+.1%} · z {s['z']:+.2f}" if s else "–"

    days = len({r["day"] for r in rows}) or 1
    L = [f"# Early Elite bets (graded before {args.cutoff}, games {args.cutoff} to {args.end})", "",
         "Flat $100 per Elite bet. Each cell: bets · ROI · luck-adjusted z "
         "(+2 or more is strong evidence; -1 to +1 is indistinguishable from no edge).", "",
         "| Placed | " + " | ".join(l for l, _ in COPIES) + " |",
         "|---|" + "---|" * len(COPIES)]
    for _, _, win in WINDOWS:
        rs = [r for r in rows if r["window"] == win]
        L.append(f"| {win} | " + " | ".join(cell(stats(rs, l)) for l, _ in COPIES) + " |")
    early = [r for r in rows if r["window"] in ("7+ days early", "3-7 days early", "2-3 days early")]
    L.append("| **48h+ combined** | " + " | ".join(cell(stats(early, l)) for l, _ in COPIES) + " |")
    L += ["", f"48h+ signals: {len(early)} over {days} active days "
               f"(about {len(early) / max(1, (len({r['day'] for r in early}) or 1)):.1f} per day they occur)", "",
          "## 48h+ by sport (copied 1 hour later)", "", "| Sport | Bets · ROI · z |", "|---|---|"]
    for sp in sorted({r["sport"] for r in early}):
        L.append(f"| {sp} | {cell(stats([r for r in early if r['sport'] == sp], '1 hour later'))} |")
    L += ["", "## 48h+ by bet type (copied 1 hour later)", "", "| Type | Bets · ROI · z |", "|---|---|"]
    for t in sorted({r["type"] for r in early}):
        L.append(f"| {t} | {cell(stats([r for r in early if r['type'] == t], '1 hour later'))} |")
    (ROOT / "EARLY.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
