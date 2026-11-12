"""Activity feed: every bet graded Sharp/Elite wallets place on upcoming
games, newest first.

Trades come in from each scan (last 24h, $200+). Fills from the same wallet
on the same side within a few minutes are merged into one row. Positions
spotted through top holders (bought earlier) are added once, when first seen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .markets import pick_label


def _key(t: dict) -> str:
    return ":".join(str(t.get(k, "")) for k in
                    ("transaction_hash", "proxy_wallet", "condition_id", "outcome_index", "side", "size", "timestamp"))


def record(log: dict, trades: list[dict], holders_cache: dict, tracked: set[str],
           now: datetime, keep_days: int = 7) -> None:
    rows = log.setdefault("rows", [])
    seen = log.setdefault("seen", {})
    for t in trades:
        w = (t.get("proxy_wallet") or "").lower()
        if w not in tracked:
            continue
        k = _key(t)
        if k in seen:
            continue
        seen[k] = int(t.get("timestamp", 0))
        rows.append({"ts": int(t.get("timestamp", 0)), "w": w, "cid": t.get("condition_id"),
                     "i": int(t.get("outcome_index", -1)), "side": t.get("side"),
                     "shares": float(t["size"]), "price": float(t["price"]), "via": "trade"})
    # positions first spotted in top holders (bought before our window)
    for cid, c in holders_cache.items():
        for r in c.get("rows", []):
            k = f"hold:{r['w']}:{cid}:{r['i']}"
            if k in seen or r["w"] not in tracked:
                continue
            seen[k] = int(now.timestamp())
            rows.append({"ts": int(now.timestamp()), "w": r["w"], "cid": cid, "i": r["i"],
                         "side": "HOLD", "shares": r["s"], "price": r["p"], "via": "holding"})
    cutoff = int((now - timedelta(days=keep_days)).timestamp())
    log["rows"] = [r for r in rows if r["ts"] >= cutoff][-20000:]
    log["seen"] = {k: v for k, v in seen.items() if v >= cutoff}


def feed(log: dict, markets: dict, grades: dict, entity: dict, picked: set[str],
         merge_minutes: int = 10, limit: int = 400) -> list[dict]:
    """Rows for the website: merged fills, newest first, upcoming games only."""
    rows = sorted((r for r in log.get("rows", []) if r["cid"] in markets),
                  key=lambda r: (r["w"], r["cid"], r["i"], r["side"], r["ts"]))
    merged: list[dict] = []
    for r in rows:
        last = merged[-1] if merged else None
        if (last and last["w"] == r["w"] and last["cid"] == r["cid"] and last["i"] == r["i"]
                and last["side"] == r["side"] and r["side"] != "HOLD"
                and r["ts"] - last["ts_last"] <= merge_minutes * 60):
            cost = last["usd"] + r["shares"] * r["price"]
            last["shares"] += r["shares"]
            last["usd"] = cost
            last["ts_last"] = r["ts"]
            continue
        merged.append({**r, "usd": r["shares"] * r["price"], "ts_last": r["ts"]})

    out = []
    for r in merged:
        m = markets[r["cid"]]
        g = (grades.get(r["w"]) or {}).get(m["sport"]) or {}
        if g.get("tier") not in ("SHARP", "ELITE"):
            continue
        idx = r["i"]
        out.append({
            "ts": datetime.fromtimestamp(r["ts_last"], timezone.utc).isoformat(),
            "wallet": r["w"], "person": entity.get(r["w"], r["w"]),
            "tier": g["tier"], "roi": g.get("shrunk_roi"), "n": g.get("n"),
            "sport": m["sport"], "market_type": m.get("market_type", "moneyline"),
            "pick": pick_label(m, idx) if idx >= 0 else "?",
            "event_title": m["event_title"], "url": m["url"], "game_start": m["game_start"],
            "side": r["side"], "via": r["via"], "usd": round(r["usd"], 2),
            "price": round(r["usd"] / r["shares"], 4) if r["shares"] else r["price"],
            "now": m["prices"][idx] if 0 <= idx < len(m["prices"]) else None,
            "is_pick": f"{r['cid']}:{idx}" in picked,
        })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:limit]
