"""Top holders: catch graded wallets that are already in a position,
including ones who bought days ago or quietly over time, which the
24-hour trade window misses.

Uses /v2/holders?include_pnl=true, which gives each holder's share count
and average entry price per outcome.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import api
from .markets import parse_time
from .signals import SideFlow

log = logging.getLogger(__name__)


def refresh(cache: dict, upcoming: list[dict], tracked: set[str], now: datetime, cfg: dict) -> list[str]:
    """Refresh holders for markets starting soon (oldest first, capped per run).
    Stores only tracked wallets plus big unknown holders. Returns wallets to
    queue for grading."""
    h = cfg["holders"]
    horizon = now + timedelta(hours=h["hours_before_start"])
    due = []
    for m in upcoming:
        start = parse_time(m["game_start"])
        if not start or start > horizon:
            continue
        c = cache.get(m["condition_id"])
        if c and now - datetime.fromisoformat(c["at"]) < timedelta(minutes=h["refresh_minutes"]):
            continue
        due.append((c["at"] if c else "", m["condition_id"]))
    due = [cid for _, cid in sorted(due)][: h["max_markets_per_run"]]

    def one(cid):
        try:
            return cid, api.holders(cid, h["per_outcome"])
        except Exception as e:
            log.warning("holders %s: %s", cid, e)
            return cid, None

    discover = set()
    with ThreadPoolExecutor(6) as ex:
        for cid, rows in ex.map(one, due):
            if rows is None:
                continue
            keep = []
            for r in rows:
                w = (r.get("proxy_wallet") or "").lower()
                idx = r.get("outcome_index")
                shares = float(r.get("amount") or 0)
                avg = float(r.get("avg_price") or 0)
                cost = float(r.get("entry_cost_usdc") or shares * avg)
                if not w or idx in (None, 999) or shares <= 0:
                    continue
                if w in tracked:
                    keep.append({"w": w, "i": int(idx), "s": round(shares, 2), "p": round(avg, 4)})
                elif cost >= h["discover_usd"]:
                    discover.add(w)
            cache[cid] = {"at": now.isoformat(), "rows": keep}
    # drop markets that are no longer upcoming
    live = {m["condition_id"] for m in upcoming}
    for cid in [c for c in cache if c not in live]:
        del cache[cid]
    log.info("holders: refreshed %d markets, %d cached", len(due), len(cache))
    return sorted(discover)


def merge(flows: dict, cache: dict, markets: dict, grades: dict) -> int:
    """Add held positions into the trade flows. A wallet holding both sides
    is netted (hedgers/market makers mostly cancel out). For each wallet the
    larger of 'recent net buying' and 'current net holding' is used."""
    added = 0
    for cid, c in cache.items():
        m = markets.get(cid)
        if not m:
            continue
        sport = m["sport"]
        per_w: dict[str, dict] = {}
        for r in c["rows"]:
            g = (grades.get(r["w"]) or {}).get(sport) or {}
            if g.get("tier") not in ("SHARP", "ELITE"):
                continue
            per_w.setdefault(r["w"], {})[r["i"]] = (r["s"], r["p"])
        for w, sides in per_w.items():
            idx = max(sides, key=lambda i: sides[i][0])
            other = sum(s for i, (s, _) in sides.items() if i != idx)
            net = sides[idx][0] - other
            avg = sides[idx][1]
            if net <= 0 or avg <= 0:
                continue
            usd = net * avg
            f = flows.setdefault((cid, idx), SideFlow(cid, idx, (m["outcomes"] + [""] * 9)[idx]))
            if usd > f.wallets.get(w, 0):
                f.wallets[w] = usd
                f.cost[w] = usd
                f.bought[w] = net
                f.from_holders = getattr(f, "from_holders", set()) | {w}
                added += 1
    return added
