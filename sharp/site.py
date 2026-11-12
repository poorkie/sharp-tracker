"""Builds docs/data.json, which the website (docs/index.html) reads."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .ledger import summary, summary_locked
from .markets import pick_label


def live_board(flows: dict, markets: dict, grades: dict, alerted_keys: set,
               view_lookup, margin: float = 0.03, entity: dict | None = None) -> list[dict]:
    entity = entity or {}
    """Every side of every upcoming game that has sharp money on it,
    whether or not it has crossed the alert threshold yet."""
    by_market = defaultdict(list)
    for (cid, _), f in flows.items():
        by_market[cid].append(f)
    rows = []
    for cid, sides in by_market.items():
        m = markets.get(cid)
        if not m:
            continue
        for f in sides:
            longs = {w: v for w, v in f.wallets.items() if v > 0}
            if not longs:
                continue
            idx = f.outcome_index
            price = m["prices"][idx] if idx < len(m["prices"]) else None
            opposing = sum(o.total for o in sides if o.outcome_index != idx)
            sport = m["sport"]
            rows.append({
                "key": f"{cid}:{idx}",
                "alerted": f"{cid}:{idx}" in alerted_keys,
                "sport": sport,
                "event_title": m["event_title"],
                "outcome": pick_label(m, idx, f.outcome),
                "market_type": m.get("market_type", "moneyline"),
                "price": price,
                **_books(view_lookup(m, idx)),
                "game_start": m["game_start"],
                "url": m["url"],
                "sharp_usd": round(sum(longs.values()), 2),
                "elites": sum(1 for w in longs if grades[w][sport]["tier"] == "ELITE"),
                "opposing_usd": round(opposing, 2),
                "wallets": sorted(
                    ({"wallet": w, "usd": round(v, 2), "entry": round(f.avg_entry(w), 3),
                      "person": entity.get(w, w),
                      "via": "holding" if w in getattr(f, "from_holders", set()) else "trade",
                      "tier": grades[w][sport]["tier"], "roi": grades[w][sport]["shrunk_roi"],
                      "n": grades[w][sport]["n"]} for w, v in longs.items()),
                    key=lambda x: -x["usd"]),
            })
    for r in rows:
        persons = [w["person"] for w in r["wallets"] if w["tier"] == "ELITE"]
        r["elite_people"] = len(set(persons))
        r["linked"] = len(persons) != len(set(persons))
        el = [w for w in r["wallets"] if w["tier"] == "ELITE" and w["entry"]] or \
             [w for w in r["wallets"] if w["entry"]]
        usd = sum(w["usd"] for w in el)
        r["max_prob"] = round(min(sum(w["usd"] * w["entry"] for w in el) / usd + margin, 0.97), 4) if usd else None
    rows.sort(key=lambda r: (not r["alerted"], -len(r["wallets"]), -r["sharp_usd"]))
    return rows


def _books(view) -> dict:
    if not view:
        return {"book_fair": None, "books": []}
    return {"book_fair": view.get("fair"), "books": (view.get("lines") or [])[:5]}


def sharps_table(wallets: dict, limit: int = 200) -> list[dict]:
    rows = []
    for w, v in wallets.items():
        for sport, g in v["grades"].items():
            if g["tier"] in ("SHARP", "ELITE"):
                rows.append({"wallet": w, "sport": sport, **g})
    rows.sort(key=lambda r: (r["tier"] != "ELITE", -r["shrunk_roi"]))
    return rows[:limit]


def write(path: Path, now: datetime, live: list, wallets: dict, book: list,
          n_markets: int, queue_len: int, min_wallets: int = 3,
          markets_by_sport: dict | None = None, games: int = 0,
          links: list | None = None, lock_minutes: int = 15,
          activity: list | None = None) -> None:
    by_sport, by_kind, by_type = defaultdict(list), defaultdict(list), defaultdict(list)
    for e in book:
        by_sport[e["sport"]].append(e)
        by_kind[e["kind"]].append(e)
        by_type[e.get("market_type", "moneyline")].append(e)
    sharps = sharps_table(wallets)
    by_key = {f"{e['condition_id']}:{e['outcome_index']}": e for e in book}
    for r in live:
        e = by_key.get(r["key"])
        r["lock"] = e.get("lock") if e else None
        r["max_prob"] = (e.get("max_prob") if e and e.get("max_prob") else r.get("max_prob"))
    linked_wallets = {w for l in (links or []) for w in (l["a"], l["b"])}
    data = {
        "updated": now.isoformat(),
        "stats": {
            "markets": n_markets,
            "games": games,
            "lock_minutes": lock_minutes,
            "linked_wallets": len(linked_wallets),
            "elites": sum(1 for v in wallets.values()
                          if any(g.get("tier") == "ELITE" for g in v["grades"].values())),
            "graded": len(wallets),
            "sharps": len({r["wallet"] for r in sharps}),
            "queue": queue_len,
            "min_wallets": min_wallets,
            "markets_by_sport": markets_by_sport or {},
        },
        "live": live,
        "sharps": sharps,
        "record": {
            "overall": summary(book),
            "locked": summary_locked(book),
            "locked_by_kind": {k: summary_locked(v) for k, v in sorted(by_kind.items())},
            "by_sport": {k: summary(v) for k, v in sorted(by_sport.items())},
            "by_kind": {k: summary(v) for k, v in sorted(by_kind.items())},
            "by_type": {k: summary(v) for k, v in sorted(by_type.items())},
        },
        "activity": activity or [],
        "alerts": sorted(book, key=lambda e: e["alerted_at"], reverse=True)[:300],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")))
