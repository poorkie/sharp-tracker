"""Copy-delay retest with REALISTIC follower prices (v2).

v1 priced a follower at the next trade of any kind, which mixes in sellers
hitting bids, cheaper than what a buyer pays. Live paper trading showed
followers paying ~3.4c above the Elite price. Here the follower's price is
the next TAKER BUY on that side after the delay: what a buyer actually paid
crossing the ask. Elite signals still come from all their buys.

Can a follower still capture the Elite in-game edge if they copy late?

For test-period games, every Elite BUY is turned into a "signal" (fills from
the same wallet on the same side within 2 minutes are merged; buys that
just hedge an existing position on the other side are skipped). A follower
copies each signal with a flat $100 at the price of the next real trade on
that side after a delay of 0s, 30s, 1m, 2m, 5m or 10m.

    python copydelay.py --cutoff 2026-09-01 --end 2026-11-10
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
log = logging.getLogger("copydelay")
DELAYS = [5, 30, 60, 120, 300, 600]
MAX_WAIT = 300   # if no trade within delay + 5 min, the follower couldn't fill


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
    log.info("%d markets", len(cids))
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
            trades = api.trades_for_conditions([cid], 200, st - 10 * 86400, max_rows=80000)
        except Exception as e:
            log.warning("trades %s: %s", cid, e)
            return []
        trades.sort(key=lambda t: int(t["timestamp"]))
        sport = sport_of[cid]
        try:
            taker = api.trades_for_conditions([cid], 1, st - 86400, max_rows=80000, taker_only=True)
        except Exception as e:
            log.warning("taker trades %s: %s", cid, e)
            return []
        # buyer's tape per side: (ts, price) of taker BUYS = what crossing the ask cost
        tape = defaultdict(list)
        for t in sorted(taker, key=lambda t: int(t["timestamp"])):
            idx = int(t.get("outcome_index", -1))
            if t.get("side") == "BUY" and 0 <= idx < len(toks):
                tape[idx].append((int(t["timestamp"]), float(t["price"])))
        tape_ts = {i: [x[0] for x in v] for i, v in tape.items()}

        held = defaultdict(lambda: defaultdict(float))   # wallet -> idx -> shares
        signals, last = [], {}
        for t in trades:
            w = (t.get("proxy_wallet") or "").lower()
            idx = int(t.get("outcome_index", -1))
            if not 0 <= idx < len(toks):
                continue
            size, price, ts = float(t["size"]), float(t["price"]), int(t["timestamp"])
            is_elite = tier.get((w, sport)) == "ELITE"
            if t.get("side") == "BUY":
                mine = held[w][idx]
                hedge = any(s > mine + 1 for j, s in list(held[w].items()) if j != idx)
                held[w][idx] += size
                if not is_elite or hedge:
                    continue
                key = (w, idx)
                if key in last and ts - last[key]["ts_last"] <= 120:
                    last[key]["usd"] += size * price
                    last[key]["ts_last"] = ts
                    continue
                sig = {"ts": ts, "ts_last": ts, "idx": idx, "p": price, "usd": size * price,
                       "phase": "in-game" if ts >= st else ("pre-game" if ts < st - 900 else "last 15 min")}
                signals.append(sig)
                last[key] = sig
            else:
                held[w][idx] = max(0.0, held[w][idx] - size)

        out = []
        for s in signals:
            idx = s["idx"]
            row = {"phase": s["phase"], "sport": sport, "won": prices[idx] >= 0.99,
                   "type": m.get("sportsMarketType") or "moneyline", "day": s["ts"] // 86400}
            row["elite"] = s["p"]
            for d in DELAYS:
                ts_list = tape_ts.get(idx, [])
                k = bisect.bisect_left(ts_list, s["ts_last"] + d)
                if k < len(ts_list) and ts_list[k] <= s["ts_last"] + d + MAX_WAIT:
                    row[d] = tape[idx][k][1]
                else:
                    row[d] = None
            out.append(row)
        return out

    with ThreadPoolExecutor(args.workers) as ex:
        rows = [r for batch in ex.map(one, cids) for r in batch]
    log.info("%d Elite signals", len(rows))

    def stats(rs, d):
        rs = [r for r in rs if r.get(d) is not None and 0.03 <= r[d] <= 0.97]
        n = len(rs)
        if not n:
            return None
        w = sum(r["won"] for r in rs)
        pnl = sum(100 * (1 - r[d]) / r[d] if r["won"] else -100 for r in rs)
        exp = sum(r[d] for r in rs)
        sd = math.sqrt(sum(r[d] * (1 - r[d]) for r in rs)) or 1
        return {"n": n, "w": w, "roi": pnl / (100 * n), "z": (w - exp) / sd, "units": pnl / 100}

    def table(rs):
        L = ["| Copy delay | Bets | Record | Units | ROI | Luck-adjusted z |", "|---|---|---|---|---|---|"]
        s0 = stats(rs, "elite")
        if s0:
            L.append(f"| (Elite's own price, reference) | {s0['n']:,} | {s0['w']:,}-{s0['n'] - s0['w']:,} | "
                     f"{s0['units']:+,.1f} | {s0['roi']:+.1%} | {s0['z']:+.2f} |")
        for d in DELAYS:
            s = stats(rs, d)
            label = (f"{d}s later" if d < 60 else f"{d // 60} min later")
            L.append(f"| {label} | " + (f"{s['n']:,} | {s['w']:,}-{s['n'] - s['w']:,} | {s['units']:+,.1f} | "
                                         f"{s['roi']:+.1%} | {s['z']:+.2f} |" if s else "0 | | | | |"))
        return L

    days = len({r["day"] for r in rows}) or 1
    L = [f"# Copy-delay test v2, buyer prices (graded before {args.cutoff}, games {args.cutoff} to {args.end})", "",
         "Flat $100 per Elite bet, filled at the next TAKER BUY on that side after the delay "
         "(what a buyer actually paid crossing the ask). "
         "z = wins above what the fill prices implied, in standard deviations (+2 or more is strong evidence).", ""]
    for phase in ("in-game", "pre-game"):
        rs = [r for r in rows if r["phase"] == phase]
        L += [f"## {phase.capitalize()} Elite bets ({len(rs):,} signals, about {len(rs) / days:.0f} per day)", ""]
        L += table(rs) + [""]
    ig = [r for r in rows if r["phase"] == "in-game"]
    slips = [r[5] - r["elite"] for r in ig if r.get(5) is not None]
    if slips:
        L += [f"Average price a 5-second follower paid above the Elite price: {sum(slips) / len(slips) * 100:+.1f}¢", ""]
    for d, lbl in ((5, "5 seconds"), (120, "2 minutes")):
        L += [f"## In-game, {lbl} later, by bet type", "", "| Type | Bets | ROI | z |", "|---|---|---|---|"]
        for t in sorted({r["type"] for r in ig}):
            s = stats([r for r in ig if r["type"] == t], d)
            if s:
                L.append(f"| {t} | {s['n']:,} | {s['roi']:+.1%} | {s['z']:+.2f} |")
        L.append("")
    L += ["## In-game, 1 minute later, by sport", "", "| Sport | Bets | ROI | z |", "|---|---|---|---|"]
    for sp in sorted({r["sport"] for r in ig}):
        s = stats([r for r in ig if r["sport"] == sp], 60)
        if s:
            L.append(f"| {sp} | {s['n']:,} | {s['roi']:+.1%} | {s['z']:+.2f} |")
    L += ["", "## In-game, 1 minute later, by bet type", "", "| Type | Bets | ROI | z |", "|---|---|---|---|"]
    for t in sorted({r["type"] for r in ig}):
        s = stats([r for r in ig if r["type"] == t], 60)
        if s:
            L.append(f"| {t} | {s['n']:,} | {s['roi']:+.1%} | {s['z']:+.2f} |")
    (ROOT / "COPYDELAY2.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
