"""Turn raw trades into smart-money signals."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class SideFlow:
    condition_id: str
    outcome_index: int
    outcome: str
    wallets: dict = field(default_factory=dict)   # wallet -> net $ (sharps only)
    shares: dict = field(default_factory=dict)    # wallet -> net shares bought
    cost: dict = field(default_factory=dict)      # wallet -> $ spent on buys
    bought: dict = field(default_factory=dict)    # wallet -> shares bought

    @property
    def total(self) -> float:
        return sum(v for v in self.wallets.values() if v > 0)

    def avg_entry(self, w: str) -> float:
        return self.cost[w] / self.bought[w] if self.bought.get(w) else 0.0


def net_flows(trades: list[dict], grades: dict, sport_of_cond: dict) -> dict:
    """Net dollars per (market, side) per graded-sharp wallet.

    Buys add, sells subtract, so a wallet scalping both directions nets
    out to ~0 and doesn't count as conviction.
    grades: wallet -> {sport -> grade dict}
    """
    flows: dict[tuple, SideFlow] = {}
    for t in trades:
        w = (t.get("proxy_wallet") or "").lower()
        cid = t.get("condition_id")
        sport = sport_of_cond.get(cid)
        g = (grades.get(w) or {}).get(sport) or {}
        if g.get("tier") not in ("SHARP", "ELITE"):
            continue
        idx = int(t.get("outcome_index", 999))
        if idx == 999:
            continue
        key = (cid, idx)
        f = flows.setdefault(key, SideFlow(cid, idx, t.get("outcome", "")))
        cash = float(t["size"]) * float(t["price"])
        sign = 1 if t.get("side") == "BUY" else -1
        f.wallets[w] = f.wallets.get(w, 0.0) + sign * cash
        if sign > 0:
            f.cost[w] = f.cost.get(w, 0.0) + cash
            f.bought[w] = f.bought.get(w, 0.0) + float(t["size"])
    return flows


def find_signals(flows: dict, markets: dict, grades: dict, cfg: dict) -> list[dict]:
    """Apply consensus + single-elite rules. markets: condition_id -> market."""
    c = cfg["consensus"]
    se = cfg["single_elite"]
    band = cfg["price_band"]
    by_market: dict[str, list[SideFlow]] = defaultdict(list)
    for (cid, _), f in flows.items():
        by_market[cid].append(f)

    signals = []
    for cid, sides in by_market.items():
        m = markets.get(cid)
        if not m:
            continue
        for f in sides:
            longs = {w: v for w, v in f.wallets.items() if v > 0}
            if not longs:
                continue
            opposing = sum(o.total for o in sides if o.outcome_index != f.outcome_index)
            price = (m["prices"][f.outcome_index]
                     if f.outcome_index < len(m["prices"]) else None)
            if price is None or not (band["min"] <= price <= band["max"]):
                continue
            sport = m["sport"]
            wallet_info = sorted(
                ({"wallet": w, "usd": round(v, 2), "entry": round(f.avg_entry(w), 3),
                  **{k: grades[w][sport][k] for k in ("tier", "shrunk_roi", "n", "t_stat")}}
                 for w, v in longs.items()),
                key=lambda x: -x["usd"])
            total = sum(longs.values())
            kind = None
            if (len(longs) >= c["min_wallets"] and total >= c["min_total_usd"]
                    and opposing <= c["max_opposing_ratio"] * total):
                kind = "CONSENSUS"
            elif se["enabled"]:
                big = [x for x in wallet_info if x["tier"] == "ELITE" and x["usd"] >= se["min_usd"]]
                if big and opposing <= c["max_opposing_ratio"] * total:
                    kind = "ELITE"
            if not kind:
                continue
            signals.append({
                "kind": kind, "condition_id": cid, "outcome_index": f.outcome_index,
                "outcome": f.outcome or m["outcomes"][f.outcome_index],
                "sharp_usd": round(total, 2), "opposing_usd": round(opposing, 2),
                "wallets": wallet_info, "price": price, "market": m,
            })
    return signals


def should_alert(sig: dict, sent: dict, growth: float) -> bool:
    """Dedupe: only re-alert the same side if sharp money grew enough."""
    key = f"{sig['condition_id']}:{sig['outcome_index']}"
    prev = sent.get(key)
    if prev is None:
        return True
    return sig["sharp_usd"] >= prev * (1 + growth)
