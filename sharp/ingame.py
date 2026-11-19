"""In-game paper trading: follow Elite bets placed DURING games.

Why: the copy-delay test (Nov 18, games Sep 1 - Nov 10) found Elite in-game
bets copied 1-5 minutes late still returned about +9% over ~1,250 bets
(z about +3.2), while their pre-game bets showed no edge.

Each scan:
  1. Pull recent trades on games in progress.
  2. Find Elite BUYs (fills within 2 min merged; buys that just hedge a
     bigger position on the other side are skipped).
  3. Log ONE paper pick per market side at the price a follower could buy
     at right now (best ask on Polymarket's order book), with the delay
     since the Elite bet recorded.
  4. Grade picks once the market resolves. Flat $100 per pick.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import api
from .markets import pick_label

log = logging.getLogger(__name__)


def detect(trades: list[dict], live: dict, grades: dict, entity: dict,
           book: list, now: datetime, cfg: dict) -> list[dict]:
    """Return Elite in-game bets not yet turned into picks.
    live: condition_id -> live market dict."""
    ig = cfg["ingame"]
    max_age = ig["max_delay_minutes"] * 60
    now_ts = int(now.timestamp())
    trades = sorted(trades, key=lambda t: int(t.get("timestamp", 0)))
    bought = defaultdict(lambda: defaultdict(float))       # (w, cid) -> idx -> shares
    groups: dict[tuple, dict] = {}
    for t in trades:
        cid = t.get("condition_id")
        m = live.get(cid)
        if not m or t.get("side") != "BUY":
            continue
        w = (t.get("proxy_wallet") or "").lower()
        idx = int(t.get("outcome_index", -1))
        if not 0 <= idx < len(m["outcomes"]):
            continue
        ts = int(t["timestamp"])
        if ts < int(datetime.fromisoformat(m["game_start"]).timestamp()):
            continue                                       # pre-game trade
        size, price = float(t["size"]), float(t["price"])
        mine = bought[(w, cid)][idx]
        hedge = any(s > mine + 1 for j, s in list(bought[(w, cid)].items()) if j != idx)
        bought[(w, cid)][idx] += size
        g = (grades.get(w) or {}).get(m["sport"]) or {}
        if g.get("tier") != "ELITE" or hedge:
            continue
        key = (w, cid, idx)
        cur = groups.get(key)
        if cur and ts - cur["ts_last"] <= 120:
            cur["usd"] += size * price
            cur["shares"] += size
            cur["ts_last"] = ts
        else:  # first bet, or a separate later bet: keep the most recent one
            groups[key] = {"w": w, "cid": cid, "idx": idx, "ts_first": ts, "ts_last": ts,
                           "usd": size * price, "shares": size, "grade": g}
    out = []
    for (w, cid, idx), g in groups.items():
        if g["usd"] < ig["min_usd"] or now_ts - g["ts_last"] > max_age:
            continue
        g["elite_price"] = g["usd"] / g["shares"]
        g["person"] = entity.get(w, w)
        out.append(g)
    return sorted(out, key=lambda x: x["ts_last"])


def record(book: list, sigs: list[dict], live: dict, now: datetime, cfg: dict,
           ask=None) -> list[dict]:
    """Turn new Elite in-game bets into paper picks. One pick per market
    side; extra Elite bettors on the same side are added to it. A side that
    conflicts with an existing pick on the same market is skipped."""
    band = cfg["ingame"]["price_band"]
    ask = ask or api.best_ask
    new = []
    for s in sigs:
        m = live[s["cid"]]
        same = next((e for e in book if e["cid"] == s["cid"] and e["idx"] == s["idx"]), None)
        if same:
            if s["w"] not in same["elite_wallets"]:
                same["elite_wallets"].append(s["w"])
                same["elite_usd"] = round(same["elite_usd"] + s["usd"], 2)
            continue
        if any(e["cid"] == s["cid"] for e in book):
            continue                                       # Elite on both sides: stay out
        tok = (m.get("tokens") or [None] * 9)[s["idx"]]
        fill = None
        if tok:
            try:
                fill = ask(tok)
            except Exception as e:
                log.warning("book %s: %s", tok, e)
        if fill is None:
            continue
        price, size = fill
        if not band["min"] <= price <= band["max"]:
            continue
        e = {"id": f"{s['cid'][:10]}-{s['idx']}-{int(now.timestamp())}",
             "alerted_at": now.isoformat(), "sport": m["sport"], "market_type": m["market_type"],
             "event_title": m["event_title"], "pick": pick_label(m, s["idx"]), "url": m["url"],
             "cid": s["cid"], "idx": s["idx"], "elite_wallets": [s["w"]],
             "elite_usd": round(s["usd"], 2), "elite_price": round(s["elite_price"], 4),
             "elite_roi": s["grade"].get("shrunk_roi"), "elite_n": s["grade"].get("n"),
             "elite_ts": datetime.fromtimestamp(s["ts_last"], timezone.utc).isoformat(),
             "fill_price": price, "fill_usd_available": round(price * size, 2),
             "delay_s": int(now.timestamp()) - s["ts_last"],
             "result": None, "pnl_100": None}
        book.append(e)
        new.append(e)
    return new


def copy_to_novig(e: dict, m: dict, client, cache: dict, cfg: dict) -> dict:
    """Try to place the same bet on Novig Paper: find the live match-winner
    market, buy at the best ask with an IOC order ($100 stake), and record
    what actually filled. Returns the record stored on the pick."""
    from . import novig as nv
    from .odds import team_matches
    rec = {"status": "skipped"}
    league = nv.LEAGUES.get(m["sport"])
    if not league or m["market_type"] != "moneyline" or len(m["outcomes"]) != 2:
        rec["reason"] = "no Novig equivalent (map/game market or unmapped league)"
        return rec
    try:
        if league not in cache:
            cache[league] = client.live_money_markets(league)
        pick, other = m["outcomes"][e["idx"]], m["outcomes"][1 - e["idx"]]
        start = datetime.fromisoformat(m["game_start"]).timestamp() * 1000
        market = outcome = None
        for nm in cache[league]:
            outs = nm.get("outcomes") or []
            if len(outs) != 2:
                continue
            a = next((o for o in outs if team_matches(pick, o["name"])), None)
            b = next((o for o in outs if o is not a and team_matches(other, o["name"])), None)
            starts = nm.get("startsTs") or start
            if a and b and abs(starts - start) <= 4 * 3600 * 1000:
                market, outcome = nm, a
                break
        if not market:
            rec["reason"] = "game not found live on Novig"
            return rec
        rec.update({"market_id": market["marketId"], "outcome": outcome["name"]})
        ask = nv.best_ask(client.book(market["marketId"]), outcome["outcomeId"])
        if not ask:
            rec.update(status="no_liquidity", reason="nothing offered on Novig")
            return rec
        price, avail = ask
        rec.update(ask=price, ask_contracts=avail)
        band = cfg["ingame"]["price_band"]
        if not band["min"] <= price <= band["max"]:
            rec.update(status="skipped", reason="Novig price outside band")
            return rec
        stake = cfg["ingame"].get("novig", {}).get("stake_usd", 100)
        qty = max(1, round(stake * 100 / price))
        order = client.place(outcome["outcomeId"], nv.on_grid(price), qty, "IOC")
        rec["order_id"] = order.get("orderId")
        import time as _t
        fills = []
        for _ in range(4):
            _t.sleep(1.5)
            fills = client.fills_for_order(rec["order_id"]) or []
            if fills:
                break
        filled = sum(int(f["qty"]) for f in fills)
        cost = sum(float(f["cost"]) for f in fills)
        fee = sum(float(f.get("fee") or 0) for f in fills)
        rec.update(status="filled" if filled >= qty else ("partial" if filled else "no_fill"),
                   wanted_contracts=qty, filled_contracts=filled, cost=round(cost, 2),
                   fee=round(fee, 4), avg_price=round(100 * cost / filled, 4) if filled else None)
    except Exception as ex:
        rec.update(status="error", reason=str(ex)[:200])
    return rec


def grade(book: list, now: datetime) -> int:
    pending = [e for e in book if e["result"] is None
               and datetime.fromisoformat(e["alerted_at"]) < now - timedelta(minutes=30)]
    if not pending:
        return 0
    try:
        closed = api.markets_by_condition(list({e["cid"] for e in pending}), closed=True)
    except Exception as ex:
        log.error("in-game result lookup failed: %s", ex)
        return 0
    by = {m.get("conditionId"): m for m in closed}
    n = 0
    for e in pending:
        m = by.get(e["cid"])
        if not m:
            continue
        prices = [float(p) for p in api.parse_json_list(m.get("outcomePrices"))]
        if e["idx"] >= len(prices):
            continue
        p = prices[e["idx"]]
        if p >= 0.99:
            e["result"], e["pnl_100"] = "WIN", round(100 * (1 - e["fill_price"]) / e["fill_price"], 2)
        elif p <= 0.01:
            e["result"], e["pnl_100"] = "LOSS", -100.0
        else:
            e["result"], e["pnl_100"] = "VOID", 0.0
        nv = e.get("novig") or {}
        if nv.get("filled_contracts"):
            payout = nv["filled_contracts"] * 0.01 if e["result"] == "WIN" else \
                (nv["cost"] if e["result"] == "VOID" else 0.0)
            pnl = payout - nv["cost"] - nv.get("fee", 0)
            nv["pnl"] = round(pnl, 2)
            nv["roi"] = round(pnl / nv["cost"], 4) if nv["cost"] else None
        n += 1
    return n


def summary(rows: list) -> dict:
    done = [r for r in rows if r["result"] in ("WIN", "LOSS")]
    n = len(done)
    w = sum(r["result"] == "WIN" for r in done)
    pnl = sum(r["pnl_100"] for r in done)
    delays = [r["delay_s"] for r in rows]
    return {"n": n, "w": w, "l": n - w, "units": round(pnl / 100, 2),
            "roi": round(pnl / (100 * n), 4) if n else 0.0,
            "pending": sum(r["result"] is None for r in rows),
            "avg_delay_min": round(sum(delays) / len(delays) / 60, 1) if delays else None}


def novig_summary(rows: list) -> dict:
    tried = [r for r in rows if r.get("novig")]
    filled = [r for r in tried if (r["novig"].get("filled_contracts") or 0) > 0]
    done = [r for r in filled if r["novig"].get("pnl") is not None]
    cost = sum(r["novig"]["cost"] for r in done)
    pnl = sum(r["novig"]["pnl"] for r in done)
    gaps = [r["novig"]["avg_price"] - r["fill_price"] for r in filled if r["novig"].get("avg_price")]
    reasons = defaultdict(int)
    for r in tried:
        reasons[r["novig"].get("status")] += 1
    return {"tried": len(tried), "filled": len(filled), "graded": len(done),
            "w": sum(r["result"] == "WIN" for r in done), "l": sum(r["result"] == "LOSS" for r in done),
            "staked": round(cost, 2), "pnl": round(pnl, 2), "roi": round(pnl / cost, 4) if cost else 0.0,
            "fees": round(sum(r["novig"].get("fee", 0) for r in filled), 2),
            "avg_price_vs_poly": round(sum(gaps) / len(gaps), 4) if gaps else None,
            "fill_rate": round(sum(r["novig"]["filled_contracts"] for r in filled) /
                               max(1, sum(r["novig"]["wanted_contracts"] for r in filled)), 3) if filled else None,
            "statuses": dict(reasons)}


def site_data(book: list) -> dict:
    by_sport = defaultdict(list)
    for e in book:
        by_sport[e["sport"]].append(e)
    return {"record": summary(book), "novig": novig_summary(book),
            "by_sport": {k: summary(v) for k, v in sorted(by_sport.items())},
            "picks": sorted(book, key=lambda e: e["alerted_at"], reverse=True)[:300]}


def format_alert(e: dict) -> tuple[str, str]:
    from .odds import prob_to_american
    p = e["fill_price"]
    mins = e["delay_s"] / 60
    title = f"⚡ LIVE ELITE BET · {e['sport'].upper()}"
    body = "\n".join([
        f"{e['pick']} @ {p * 100:.0f}¢ ({prob_to_american(p)})",
        e["event_title"],
        f"Elite bought ${e['elite_usd']:,.0f} @ {e['elite_price'] * 100:.0f}¢, {mins:.0f} min ago",
        f"Wallet: ROI {e['elite_roi']:+.0%} over {e['elite_n']} bets" if e.get("elite_roi") is not None else "",
        "Paper trade: in-game edge is still being tested",
    ])
    return title, body.replace("\n\n", "\n")
