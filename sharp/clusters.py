"""Multi-wallet detection: is this "2 Elite wallets agree" really 2 people?

Big bettors often split money across several wallets. Two kinds of
evidence link wallets to one person:

  1. Same bets. Their recent game bets overlap far more than chance, on the
     same side, and they almost never bet against each other.
  2. Same timing. When both bet the same side of a game, they do it within
     a couple of minutes of each other, again and again.

Linked wallets are merged into one "entity", and pick rules count entities,
not wallets.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import combinations

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _h(s: str, n: int) -> str:
    return hashlib.sha1(str(s).encode()).hexdigest()[:n]


def fingerprint(rows: list[dict], sports: dict, max_bets: int = 400) -> list[str]:
    """Compact signature of a wallet's recent game bets: 'cidhash:tokenhash'."""
    out, seen = [], set()
    for r in rows:
        slug = (r.get("event_slug") or "").lower()
        if not DATE.search(slug) or slug.split("-", 1)[0] not in sports:
            continue
        cid, tok = r.get("condition_id"), r.get("token_id")
        if not cid or not tok or (cid, tok) in seen:
            continue
        seen.add((cid, tok))
        out.append(f"{_h(cid, 8)}:{_h(tok, 3)}")
        if len(out) >= max_bets:
            break
    return out


def overlap(fp_a: list[str], fp_b: list[str]) -> dict:
    a = dict(x.split(":") for x in fp_a)
    b = dict(x.split(":") for x in fp_b)
    shared_markets = a.keys() & b.keys()
    same = sum(1 for c in shared_markets if a[c] == b[c])
    return {"same": same, "opposite": len(shared_markets) - same,
            "ratio": same / max(1, min(len(a), len(b)))}


def update_cotrade(state: dict, trades: list[dict], tracked: set[str], now: datetime,
                   window_s: int = 120) -> None:
    """Record, once per (market side, wallet pair), whether two tracked
    wallets that bought the same side did it within window_s seconds."""
    pairs = state.setdefault("pairs", {})
    seen = state.setdefault("seen", {})
    buys = defaultdict(lambda: defaultdict(list))   # (cid, idx) -> wallet -> [ts]
    for t in trades:
        w = (t.get("proxy_wallet") or "").lower()
        if w in tracked and t.get("side") == "BUY":
            buys[(t.get("condition_id"), t.get("outcome_index"))][w].append(int(t.get("timestamp", 0)))
    for (cid, idx), ws in buys.items():
        for a, b in combinations(sorted(ws), 2):
            key = f"{cid}:{idx}:{a}|{b}"
            if key in seen:
                continue
            seen[key] = now.isoformat()
            gap = min(abs(x - y) for x in ws[a] for y in ws[b])
            p = pairs.setdefault(f"{a}|{b}", {"both": 0, "together": 0})
            p["both"] += 1
            p["together"] += int(gap <= window_s)
    cutoff = (now - timedelta(days=3)).isoformat()
    for k in [k for k, v in seen.items() if v < cutoff]:
        del seen[k]


def build(wallets: dict, cotrade: dict, cfg: dict) -> tuple[dict, list[dict]]:
    """Returns (wallet -> entity id, list of linked pairs with evidence)."""
    tracked = {w for w, v in wallets.items()
               if any(g.get("tier") in ("SHARP", "ELITE") for g in v["grades"].values())}
    parent = {w: w for w in tracked}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    links = []
    fps = {w: wallets[w].get("fp") or [] for w in tracked}
    for a, b in combinations(sorted(tracked), 2):
        if not fps[a] or not fps[b]:
            continue
        o = overlap(fps[a], fps[b])
        if (o["same"] >= cfg["min_shared_bets"] and o["ratio"] >= cfg["min_overlap"]
                and o["opposite"] <= cfg["max_opposite_share"] * o["same"]):
            links.append({"a": a, "b": b, "why": "same bets", **o})
    for key, p in (cotrade.get("pairs") or {}).items():
        a, b = key.split("|")
        if a in parent and b in parent and p["together"] >= cfg["min_cotimed"] \
                and p["together"] >= cfg["cotimed_share"] * p["both"]:
            links.append({"a": a, "b": b, "why": "same timing", **p})
    for l in links:
        ra, rb = find(l["a"]), find(l["b"])
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    entity = {w: find(w) for w in tracked}
    return entity, links
