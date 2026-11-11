"""Out-of-sample backtest: would following these sharps have made money?

Method
  1. Pull full resolved betting history for every wallet the tracker knows.
  2. Grade each wallet using ONLY games before the cutoff date.
  3. On games AFTER the cutoff, measure:
     A. Skill persistence: did wallets graded sharp before the cutoff keep
        winning after it, compared with wallets that weren't?
     B. Follow strategy: for every game where 3+ (pre-cutoff) sharps were on
        the same side, bet $100 at the Polymarket price 15 minutes before
        start (the "lock" price a follower could actually get).

    python backtest.py --cutoff 2026-09-01
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

from sharp import api, grading
from sharp.markets import parse_time

ROOT = Path(__file__).parent
log = logging.getLogger("backtest")
DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def fetch_history(w: str) -> list[dict]:
    try:
        rows = api.positions(w, "CLOSED", 4000) + api.positions(w, "REDEEMABLE", 2000)
    except Exception as e:
        log.warning("%s: %s", w, e)
        return []
    out, seen = [], set()
    for r in rows:
        key = (r.get("condition_id"), r.get("token_id"))
        if key in seen:
            continue
        seen.add(key)
        r["_wallet"] = w
        out.append(r)
    return out


def to_bet(r: dict, sports: dict):
    """Returns (date, Bet, row) or None."""
    slug = r.get("event_slug") or r.get("slug") or ""
    m = DATE.search(slug)
    sport = grading.sport_of(slug, sports)
    if not (m and sport):
        return None
    bets = grading.bets_from_positions([r], sports, True)
    return (m.group(1), bets[0], r) if bets else None


def group_stats(bets) -> dict:
    n = len(bets)
    staked = sum(b.cost for b in bets)
    pnl = sum(b.pnl for b in bets)
    flat = sum(b.pnl / b.cost for b in bets)
    return {
        "bets": n,
        "staked": round(staked),
        "pnl": round(pnl),
        "roi_dollar": round(pnl / staked, 4) if staked else 0,
        "roi_flat": round(flat / n, 4) if n else 0,
        "hit_rate": round(sum(b.won for b in bets) / n, 3) if n else 0,
        "avg_price": round(sum(b.entry_price for b in bets) / n, 3) if n else 0,
    }


def lock_price(token: str, start: datetime) -> float | None:
    st = int(start.timestamp())
    try:
        hist = api.price_history(token, st - 6 * 3600, st, 5)
    except Exception:
        return None
    pts = [h for h in hist if h["t"] <= st - 15 * 60]
    return float(pts[-1]["p"]) if pts else None


def run_pre_lock(test, tier, sports, ccfg, workers) -> dict:
    """Rebuild signals from timestamped trades, counting only sharp trades
    placed before kickoff minus 15 min, so in-game bets can't leak in."""
    holders = defaultdict(set)   # cid -> sharp wallets holding anything there
    sport_of_cid = {}
    for w, items in test.items():
        for b, r in items:
            if tier.get((w, b.sport)) in ("SHARP", "ELITE"):
                holders[r["condition_id"]].add(w)
                sport_of_cid[r["condition_id"]] = b.sport
    cids = [c for c, ws in holders.items() if len(ws) >= 2]
    log.info("pre-lock: %d markets with 2+ sharp holders", len(cids))
    markets = {m.get("conditionId"): m for m in api.markets_by_condition(cids, closed=True)}

    def one(cid):
        m = markets.get(cid)
        if not m:
            return []
        start = parse_time(m.get("gameStartTime")) or parse_time(m.get("eventStartTime"))
        toks = api.parse_json_list(m.get("clobTokenIds"))
        prices = [float(p) for p in api.parse_json_list(m.get("outcomePrices"))]
        if not start or len(toks) != len(prices) or not toks:
            return []
        lock_ts = int(start.timestamp()) - 15 * 60
        try:
            trades = api.trades_for_conditions([cid], 100, lock_ts - 7 * 86400, max_rows=20000)
        except Exception as e:
            log.warning("trades %s: %s", cid, e)
            return []
        sport = sport_of_cid[cid]
        flow = defaultdict(lambda: defaultdict(float))   # idx -> wallet -> net $
        cost = defaultdict(lambda: defaultdict(float))
        shares = defaultdict(lambda: defaultdict(float))
        for t in trades:
            if int(t.get("timestamp", 0)) > lock_ts:
                continue
            w = (t.get("proxy_wallet") or "").lower()
            tr = tier.get((w, sport))
            if tr not in ("SHARP", "ELITE"):
                continue
            idx = int(t.get("outcome_index", -1))
            if idx < 0:
                continue
            cash = float(t["size"]) * float(t["price"])
            if t.get("side") == "BUY":
                flow[idx][w] += cash
                cost[idx][w] += cash
                shares[idx][w] += float(t["size"])
            else:
                flow[idx][w] -= cash
        out = []
        for idx, ws in flow.items():
            longs = {w: v for w, v in ws.items() if v > 0}
            total = sum(longs.values())
            opp = sum(v for j, o in flow.items() if j != idx for v in o.values() if v > 0)
            elites = [w for w in longs if tier.get((w, sport)) == "ELITE"]
            kinds = []
            if len(longs) >= ccfg["min_wallets"] and total >= ccfg["min_total_usd"] \
                    and opp <= ccfg["max_opposing_ratio"] * total:
                kinds.append("consensus_3plus")
            if len(elites) >= 2 and sum(longs[w] for w in elites) >= 2000 \
                    and opp <= ccfg["max_opposing_ratio"] * total:
                kinds.append("elite_2plus")
            if any(longs[w] >= 5000 for w in elites) and opp <= ccfg["max_opposing_ratio"] * total:
                kinds.append("single_elite_5k")
            if not kinds:
                continue
            lp = lock_price(toks[idx], start)
            c = sum(cost[idx][w] for w in longs)
            s = sum(shares[idx][w] for w in longs)
            out.append({"cid": cid, "sport": sport, "idx": idx, "kinds": kinds,
                        "won": prices[idx] >= 0.99, "void": 0.01 < prices[idx] < 0.99,
                        "lock_price": lp, "sharp_avg": round(c / s, 3) if s else None,
                        "wallets": len(longs), "sharp_usd": round(total),
                        "market_type": m.get("sportsMarketType"), "question": m.get("question"),
                        "start": start.isoformat()})
        return out

    with ThreadPoolExecutor(workers) as ex:
        sigs = [s for batch in ex.map(one, cids) for s in batch if not s["void"]]

    def stats(rows):
        rs = [r for r in rows if r["lock_price"] and 0.08 <= r["lock_price"] <= 0.92]
        n = len(rs)
        pnl = sum(100 * (1 - r["lock_price"]) / r["lock_price"] if r["won"] else -100 for r in rs)
        mv = [r["lock_price"] - r["sharp_avg"] for r in rs if r["sharp_avg"]]
        return {"bets": n, "wins": sum(r["won"] for r in rs), "units": round(pnl / 100, 2),
                "roi": round(pnl / (100 * n), 4) if n else 0,
                "avg_price": round(sum(r["lock_price"] for r in rs) / n, 3) if n else 0,
                "avg_move_after_sharps": round(sum(mv) / len(mv), 4) if mv else None}

    res = {}
    for kind in ("consensus_3plus", "elite_2plus", "single_elite_5k"):
        rows = [s for s in sigs if kind in s["kinds"]]
        res[kind] = {"all": stats(rows),
                     "by_sport": {sp: stats([r for r in rows if r["sport"] == sp])
                                  for sp in sorted({r["sport"] for r in rows})},
                     "by_type": {t: stats([r for r in rows if str(r["market_type"]) == t])
                                 for t in sorted({str(r["market_type"]) for r in rows})}}
    res["signals"] = sigs
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-09-01")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    sports, gcfg, ccfg = cfg["sports"], cfg["grading"], cfg["consensus"]

    wallets = list(json.loads((ROOT / "state/wallets.json").read_text()))
    log.info("fetching history for %d wallets", len(wallets))
    with ThreadPoolExecutor(args.workers) as ex:
        histories = list(ex.map(fetch_history, wallets))

    train, test = defaultdict(list), defaultdict(list)   # wallet -> [(Bet, row)]
    for rows in histories:
        for r in rows:
            x = to_bet(r, sports)
            if not x:
                continue
            d, bet, row = x
            (train if d < args.cutoff else test)[row["_wallet"]].append((bet, row))
    log.info("train bets %d, test bets %d",
             sum(map(len, train.values())), sum(map(len, test.values())))

    # ---- grade on pre-cutoff games only ----
    tier = {}  # (wallet, sport) -> tier
    for w, items in train.items():
        bets = [b for b, _ in items]
        for s in {b.sport for b in bets}:
            tier[(w, s)] = grading.grade_bets(bets, s, gcfg).tier

    # ---- A: skill persistence ----
    groups = defaultdict(list)
    for w, items in test.items():
        for b, _ in items:
            groups[tier.get((w, b.sport), "UNGRADED")].append(b)
            groups[f"{b.sport}:{tier.get((w, b.sport), 'UNGRADED')}"].append(b)
    persistence = {k: group_stats(v) for k, v in sorted(groups.items())}

    # ---- C: same idea, but only counting trades placed BEFORE the lock ----
    pre_lock = run_pre_lock(test, tier, sports, ccfg, args.workers)

    # ---- B: consensus follow at lock price ----
    by_cid = defaultdict(lambda: defaultdict(dict))  # cid -> token -> wallet -> (cost, row)
    for w, items in test.items():
        for b, r in items:
            if tier.get((w, b.sport)) in ("SHARP", "ELITE"):
                by_cid[r["condition_id"]][r["token_id"]][w] = (b.cost, r, b.entry_price)
    candidates = []
    for cid, sides in by_cid.items():
        totals = {t: sum(c for c, _, _ in ws.values()) for t, ws in sides.items()}
        for tok, ws in sides.items():
            total = totals[tok]
            opp = sum(v for t, v in totals.items() if t != tok)
            if len(ws) >= ccfg["min_wallets"] and total >= ccfg["min_total_usd"] \
                    and opp <= ccfg["max_opposing_ratio"] * total:
                any_row = next(iter(ws.values()))[1]
                sharp_avg = sum(c * p for c, _, p in ws.values()) / total
                candidates.append({"cid": cid, "token": tok, "wallets": len(ws),
                                   "sharp_usd": round(total), "sharp_avg": round(sharp_avg, 3),
                                   "outcome": any_row.get("outcome"),
                                   "slug": any_row.get("event_slug"),
                                   "sport": grading.sport_of(any_row.get("event_slug"), sports)})
    log.info("%d consensus signals in test period", len(candidates))

    markets = {m.get("conditionId"): m for m in
               api.markets_by_condition(list({c["cid"] for c in candidates}), closed=True)}

    def resolve(c):
        m = markets.get(c["cid"])
        if not m:
            return None
        toks = api.parse_json_list(m.get("clobTokenIds"))
        prices = [float(p) for p in api.parse_json_list(m.get("outcomePrices"))]
        if c["token"] not in toks:
            return None
        idx = toks.index(c["token"])
        if idx >= len(prices) or 0.01 < prices[idx] < 0.99:
            return None  # void / unresolved
        start = parse_time(m.get("gameStartTime")) or parse_time(m.get("eventStartTime"))
        lp = lock_price(c["token"], start) if start else None
        won = prices[idx] >= 0.99
        return {**c, "won": won, "lock_price": lp, "start": start.isoformat() if start else None,
                "question": m.get("question"), "market_type": m.get("sportsMarketType")}

    with ThreadPoolExecutor(args.workers) as ex:
        resolved = [x for x in ex.map(resolve, candidates) if x]

    def follow(rows, price_key, band=(0.08, 0.92)):
        rs = [r for r in rows if r.get(price_key) and band[0] <= r[price_key] <= band[1]]
        n = len(rs)
        pnl = sum(100 * (1 - r[price_key]) / r[price_key] if r["won"] else -100 for r in rs)
        clv = [r["lock_price"] - r["sharp_avg"] for r in rs if r.get("lock_price")]
        return {"bets": n, "wins": sum(r["won"] for r in rs),
                "units": round(pnl / 100, 2), "roi": round(pnl / (100 * n), 4) if n else 0,
                "avg_price": round(sum(r[price_key] for r in rs) / n, 3) if n else 0,
                "avg_move_after_sharps": round(sum(clv) / len(clv), 4) if clv else None}

    follow_res = {
        "at_lock_price": follow(resolved, "lock_price"),
        "at_sharp_avg_price": follow(resolved, "sharp_avg"),
        "by_sport_lock": {s: follow([r for r in resolved if r["sport"] == s], "lock_price")
                          for s in sorted({r["sport"] for r in resolved})},
        "by_type_lock": {t: follow([r for r in resolved if r["market_type"] == t], "lock_price")
                         for t in sorted({str(r["market_type"]) for r in resolved})},
        "no_lock_price": sum(1 for r in resolved if not r.get("lock_price")),
    }

    out = {"cutoff": args.cutoff, "run_at": datetime.now(timezone.utc).isoformat(),
           "wallets": len(wallets), "persistence": persistence, "follow": follow_res,
           "pre_lock": pre_lock,
           "signals": sorted(resolved, key=lambda r: r["start"] or "")}
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs/backtest.json").write_text(json.dumps(out, indent=1, default=str))

    # ---- readable summary ----
    L = [f"# Backtest (graded on games before {args.cutoff}, tested after)", "",
         f"Run {out['run_at'][:16]} UTC over {len(wallets)} wallets.", "",
         "## A. Do pre-cutoff sharps keep winning?", "",
         "| Group | Bets | Staked | P&L | ROI ($-weighted) | ROI (flat) | Hit rate | Avg price |",
         "|---|---|---|---|---|---|---|---|"]
    for k, s in persistence.items():
        L.append(f"| {k} | {s['bets']} | ${s['staked']:,} | ${s['pnl']:,} | {s['roi_dollar']:+.1%} "
                 f"| {s['roi_flat']:+.1%} | {s['hit_rate']:.1%} | {s['avg_price']:.3f} |")
    L += ["", "## B. Following 3+ sharp consensus ($100 per signal)", "",
          "| Entry price used | Bets | W | Units | ROI | Avg price | Avg move after sharps |", "|---|---|---|---|---|---|---|"]
    for k in ("at_lock_price", "at_sharp_avg_price"):
        s = follow_res[k]
        mv = f"{s['avg_move_after_sharps'] * 100:+.1f}¢" if s["avg_move_after_sharps"] is not None else "–"
        L.append(f"| {k} | {s['bets']} | {s['wins']} | {s['units']:+.2f} | {s['roi']:+.1%} | {s['avg_price']:.3f} | {mv} |")
    for title, key in (("By sport (lock price)", "by_sport_lock"), ("By market type (lock price)", "by_type_lock")):
        L += ["", f"### {title}", "", "| | Bets | W | Units | ROI |", "|---|---|---|---|---|"]
        for k, s in follow_res[key].items():
            L.append(f"| {k} | {s['bets']} | {s['wins']} | {s['units']:+.2f} | {s['roi']:+.1%} |")
    L += ["", f"Signals without a lock price (skipped in lock rows): {follow_res['no_lock_price']}", ""]
    L += ["## C. Honest version: only sharp trades placed BEFORE the 15-min lock count", "",
          "$100 per signal at the lock price. This is the number that matters.", ""]
    for kind, label in (("consensus_3plus", "3+ sharps agree"), ("elite_2plus", "2+ ELITE agree"),
                        ("single_elite_5k", "Single ELITE bet $5k+")):
        blk = pre_lock[kind]
        s = blk["all"]
        mv = f"{s['avg_move_after_sharps'] * 100:+.1f}¢" if s["avg_move_after_sharps"] is not None else "–"
        L += [f"### {label}", "",
              f"**{s['bets']} bets, {s['wins']}-{s['bets'] - s['wins']}, {s['units']:+.2f}u, ROI {s['roi']:+.1%}**, "
              f"avg lock price {s['avg_price']:.3f}, price move from sharp entry to lock {mv}", "",
              "| Split | Bets | W | Units | ROI |", "|---|---|---|---|---|"]
        for grp in ("by_sport", "by_type"):
            for k, v in blk[grp].items():
                L.append(f"| {k} | {v['bets']} | {v['wins']} | {v['units']:+.2f} | {v['roi']:+.1%} |")
        L.append("")
    (ROOT / "BACKTEST.md").write_text("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    main()
