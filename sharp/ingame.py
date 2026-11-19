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
        elif not cur:
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


def site_data(book: list) -> dict:
    by_sport = defaultdict(list)
    for e in book:
        by_sport[e["sport"]].append(e)
    return {"record": summary(book),
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
