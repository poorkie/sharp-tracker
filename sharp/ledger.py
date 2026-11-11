"""Every alert gets logged, then graded after the game.

Two scores per alert:
  * Result: flat $100 bet at the price when the alert fired.
  * CLV (closing line value): price at game start minus price at
    alert. Positive CLV over a big sample is the best evidence an
    alert source has real edge, much faster than waiting on W-L.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from . import api
from .markets import parse_time

log = logging.getLogger(__name__)


def add_alert(ledger: list, sig: dict, fair: float | None, now: datetime) -> dict:
    m = sig["market"]
    entry = {
        "id": f"{sig['condition_id'][:10]}-{sig['outcome_index']}-{int(now.timestamp())}",
        "alerted_at": now.isoformat(),
        "kind": sig["kind"],
        "sport": m["sport"],
        "event_title": m["event_title"],
        "question": m["question"],
        "url": m["url"],
        "condition_id": sig["condition_id"],
        "outcome_index": sig["outcome_index"],
        "outcome": sig["outcome"],
        "entry_price": sig["price"],
        "book_fair": fair,
        "sharp_usd": sig["sharp_usd"],
        "n_wallets": len(sig["wallets"]),
        "game_start": m["game_start"],
        "close_price": sig["price"],
        "result": None,
        "pnl_100": None,
        "clv": None,
    }
    ledger.append(entry)
    return entry


def update_closing_prices(ledger: list, markets: dict, now: datetime) -> None:
    """While a game hasn't started, keep refreshing its last price."""
    for e in ledger:
        if e["result"] is not None:
            continue
        start = parse_time(e["game_start"])
        m = markets.get(e["condition_id"])
        if m and start and now < start and e["outcome_index"] < len(m["prices"]):
            e["close_price"] = m["prices"][e["outcome_index"]]


def grade_results(ledger: list, now: datetime) -> int:
    pending = [e for e in ledger if e["result"] is None
               and (parse_time(e["game_start"]) or now) < now - timedelta(hours=3)]
    if not pending:
        return 0
    try:
        closed = api.markets_by_condition(list({e["condition_id"] for e in pending}), closed=True)
    except Exception as ex:
        log.error("result lookup failed: %s", ex)
        return 0
    by_cid = {m.get("conditionId"): m for m in closed}
    graded = 0
    for e in pending:
        m = by_cid.get(e["condition_id"])
        if not m:
            continue
        prices = [float(p) for p in api.parse_json_list(m.get("outcomePrices"))]
        if e["outcome_index"] >= len(prices):
            continue
        p = prices[e["outcome_index"]]
        if p >= 0.99:
            e["result"], e["pnl_100"] = "WIN", round(100 * (1 - e["entry_price"]) / e["entry_price"], 2)
        elif p <= 0.01:
            e["result"], e["pnl_100"] = "LOSS", -100.0
        else:
            e["result"], e["pnl_100"] = "VOID", 0.0
        e["clv"] = round(e["close_price"] - e["entry_price"], 4)
        graded += 1
    return graded


def summary(rows: list) -> dict:
    done = [r for r in rows if r["result"] in ("WIN", "LOSS")]
    n = len(done)
    w = sum(r["result"] == "WIN" for r in done)
    pnl = sum(r["pnl_100"] for r in done)
    clvs = [r["clv"] for r in rows if r["clv"] is not None]
    return {
        "n": n, "w": w, "l": n - w,
        "units": round(pnl / 100, 2),
        "roi": round(pnl / (100 * n), 4) if n else 0.0,
        "avg_clv": round(sum(clvs) / len(clvs), 4) if clvs else 0.0,
        "beat_close": round(sum(c > 0 for c in clvs) / len(clvs), 3) if clvs else 0.0,
        "pending": sum(r["result"] is None for r in rows),
    }


def render_record(ledger: list, now: datetime) -> str:
    def row(label, s):
        return (f"| {label} | {s['w']}-{s['l']} | {s['units']:+.2f}u | {s['roi']:+.1%} "
                f"| {s['avg_clv'] * 100:+.1f}¢ | {s['beat_close']:.0%} | {s['pending']} |")

    head = ("| | Record | Units ($100 = 1u) | ROI | Avg CLV | Beat close | Pending |\n"
            "|---|---|---|---|---|---|---|")
    lines = ["# Sharp Tracker: graded record", "",
             f"Updated {now.strftime('%Y-%m-%d %H:%M UTC')}. Every alert is logged at the price when "
             "it fired and graded automatically, losses included. CLV = price at game start minus "
             "alert price; consistently positive CLV is the strongest sign of real edge.", "",
             "## Overall", head, row("All alerts", summary(ledger))]
    for title, keyf in (("By signal", "kind"), ("By sport", "sport")):
        groups = defaultdict(list)
        for e in ledger:
            groups[e[keyf]].append(e)
        lines += ["", f"## {title}", head]
        lines += [row(k.upper(), summary(v)) for k, v in sorted(groups.items())]
    lines += ["", "## Last 50 alerts",
              "| Alerted (UTC) | Sport | Pick | Entry | Close | Books fair | Sharps | Result |",
              "|---|---|---|---|---|---|---|---|"]
    for e in sorted(ledger, key=lambda x: x["alerted_at"], reverse=True)[:50]:
        fair = f"{e['book_fair']:.0%}" if e.get("book_fair") else "–"
        res = e["result"] or "pending"
        if e["pnl_100"] is not None:
            res += f" ({e['pnl_100']:+.0f})"
        lines.append(f"| {e['alerted_at'][:16].replace('T', ' ')} | {e['sport'].upper()} | "
                     f"[{e['outcome']}: {e['event_title']}]({e['url']}) | {e['entry_price']:.2f} | "
                     f"{e['close_price']:.2f} | {fair} | {e['n_wallets']} / ${e['sharp_usd']:,.0f} | {res} |")
    return "\n".join(lines) + "\n"
