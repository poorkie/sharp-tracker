"""Sharp Tracker: one scan. GitHub Actions runs this every ~10 minutes.

    python run.py              # normal run
    python run.py --console    # print alerts instead of sending them
    python run.py --no-save    # don't write state (for testing)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import re

import yaml

from sharp import api, grading, ledger, markets, notify, odds, signals, site

ROOT = Path(__file__).parent
STATE = ROOT / "state"
log = logging.getLogger("sharp")


# ---------------- state helpers ----------------

def load(name: str, default):
    p = STATE / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name: str, obj) -> None:
    STATE.mkdir(exist_ok=True)
    (STATE / name).write_text(json.dumps(obj, indent=1, sort_keys=True))


# ---------------- steps ----------------

def refresh_candidates(cfg, wallets, queue, meta, now):
    last = meta.get("leaderboard_at")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=24):
        return
    added = 0
    for period in cfg["leaderboard"]["periods"]:
        try:
            rows = api.leaderboard("sports", period, cfg["leaderboard"]["limit"])
        except Exception as e:
            log.error("leaderboard %s failed: %s", period, e)
            continue
        for r in rows:
            w = (r.get("user_id") or "").lower()
            if w and w not in wallets and w not in queue:
                queue.append(w)
                added += 1
    meta["leaderboard_at"] = now.isoformat()
    log.info("leaderboard: queued %d new wallets", added)


def grade_some(cfg, wallets, queue, now):
    g = cfg["grading"]
    stale = sorted(
        (w for w, v in wallets.items()
         if now - datetime.fromisoformat(v["graded_at"]) > timedelta(days=g["regrade_days"])),
        key=lambda w: wallets[w]["graded_at"])
    todo = (queue + stale)[: g["max_new_grades_per_run"]]
    for w in todo:
        try:
            rows = api.positions(w, "CLOSED", g["history_limit"])
            rows += api.positions(w, "REDEEMABLE", g["history_limit"] // 2)
        except Exception as e:
            log.warning("grading %s failed: %s", w, e)
            continue
        wallets[w] = {"graded_at": now.isoformat(),
                      "grades": grading.grade_wallet(rows, cfg["sports"], g)}
        if w in queue:
            queue.remove(w)
    sharp = sum(any(x["tier"] != "NONE" for x in v["grades"].values()) for v in wallets.values())
    log.info("graded %d wallets this run; %d graded total, %d sharp", len(todo), len(wallets), sharp)


def book_fair_prob(cfg, m, idx, odds_cache, now, allow_fetch=True) -> float | None:
    """No-vig sportsbook probability for outcome `idx` of market `m`.
    Only spends API credits when allow_fetch is set (i.e. for alerts)."""
    key = os.environ.get("ODDS_API_KEY")
    sport_key = (cfg["sports"].get(m["sport"]) or {}).get("odds_key")
    if not (cfg["odds"]["enabled"] and sport_key and m["market_type"] == "moneyline"):
        return None
    cached = odds_cache.get(sport_key)
    stale = (not cached or now - datetime.fromisoformat(cached["at"])
             > timedelta(minutes=cfg["odds"]["cache_minutes"]))
    if stale and not (allow_fetch and key):
        return None
    if stale:
        try:
            cached = {"at": now.isoformat(),
                      "games": api.sportsbook_odds(sport_key, key, cfg["odds"]["regions"])}
            odds_cache[sport_key] = cached
        except Exception as e:
            log.warning("odds fetch failed: %s", e)
            return None
    start = markets.parse_time(m["game_start"])
    outs = m["outcomes"]
    if idx >= len(outs):
        return None
    if len(outs) == 2 and set(o.lower() for o in outs) != {"yes", "no"}:
        hit = odds.find_fair_prob(cached["games"], outs[idx], start, outs[1 - idx])
        return round(hit[0], 4) if hit else None
    mt = re.search(r"will (.+?) win", m["question"] or "", re.I)
    if mt:
        hit = odds.find_fair_prob(cached["games"], mt.group(1), start)
        if hit:
            p = hit[0] if outs[idx].lower() == "yes" else 1 - hit[0]
            return round(p, 4)
    return None


def format_alert(sig, fair, tz) -> tuple[str, str]:
    m = sig["market"]
    p = sig["price"]
    start = markets.parse_time(m["game_start"]).astimezone(tz)
    label = {"CONSENSUS": "SHARP CONSENSUS", "ELITE_CONSENSUS": "ELITE CONSENSUS"}.get(sig["kind"], "ELITE BET")
    title = f"{label} · {m['sport'].upper()}"
    lines = [
        f"{sig['outcome']} @ {p * 100:.0f}¢ ({odds.prob_to_american(p)})",
        f"{m['event_title']} · {start.strftime('%a %-I:%M %p %Z')}",
        f"{len(sig['wallets'])} sharp{'s' if len(sig['wallets']) > 1 else ''} · "
        f"${sig['sharp_usd']:,.0f} in · ${sig['opposing_usd']:,.0f} other side",
    ]
    if fair is not None:
        edge = fair - p
        lines.append(f"Books fair {fair:.0%} ({odds.prob_to_american(fair)}) · "
                     f"{'Poly cheaper' if edge > 0 else 'Poly pricier'} by {abs(edge) * 100:.1f}¢")
    for w in sig["wallets"][:3]:
        lines.append(f"{w['wallet'][:6]}… {w['tier']} ROI {w['shrunk_roi']:+.0%} "
                     f"n={w['n']} · ${w['usd']:,.0f} @ {w['entry'] * 100:.0f}¢")
    return title, "\n".join(lines)


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--console", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    tz = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    channels = ["console"] if args.console else cfg["alerts"]["channels"]
    now = datetime.now(timezone.utc)

    wallets = load("wallets.json", {})
    queue = load("queue.json", [])
    meta = load("meta.json", {})
    sent = load("sent.json", {})
    book = ledger.dedupe(load("ledger.json", []))
    odds_cache = load("odds_cache.json", {})

    refresh_candidates(cfg, wallets, queue, meta, now)
    grade_some(cfg, wallets, queue, now)

    upcoming = markets.upcoming_markets(cfg, now)
    by_cid = {m["condition_id"]: m for m in upcoming}
    since = int((now - timedelta(hours=cfg["consensus"]["window_hours"])).timestamp())
    trades = api.trades_for_conditions(list(by_cid), cfg["min_trade_usd"], since) if by_cid else []
    log.info("%d qualifying trades in window", len(trades))

    # discovery: big bettors we haven't graded yet get queued
    for t in trades:
        w = (t.get("proxy_wallet") or "").lower()
        if (float(t["size"]) * float(t["price"]) >= cfg["grading"]["discover_trade_usd"]
                and w not in wallets and w not in queue):
            queue.append(w)

    grades = {w: v["grades"] for w, v in wallets.items()}
    flows = signals.net_flows(trades, grades, {c: m["sport"] for c, m in by_cid.items()})
    found = signals.find_signals(flows, by_cid, grades, cfg)
    log.info("%d signals", len(found))

    for sig in found:
        if not signals.should_alert(sig, sent, cfg["realert_growth"]):
            continue
        if ledger.has_other_side(book, sig):
            log.info("skip %s: already alerted the other side", sig["outcome"])
            continue
        fair = book_fair_prob(cfg, sig["market"], sig["outcome_index"], odds_cache, now)
        title, body = format_alert(sig, fair, tz)
        is_update = ledger.find(book, sig) is not None
        if is_update:
            title = "UPDATE · " + title
        notify.send(channels, title, body, sig["market"]["url"])
        ledger.record_alert(book, sig, fair, now)
        sent[f"{sig['condition_id']}:{sig['outcome_index']}"] = sig["sharp_usd"]

    ledger.update_closing_prices(book, by_cid, now)
    n = ledger.grade_results(book, now)
    if n:
        log.info("graded %d finished alerts", n)

    if not args.no_save:
        save("wallets.json", wallets)
        save("queue.json", queue)
        save("meta.json", meta)
        save("sent.json", sent)
        save("ledger.json", book)
        save("odds_cache.json", odds_cache)
        (ROOT / "RECORD.md").write_text(ledger.render_record(book, now))
        live = site.live_board(
            flows, by_cid, grades, set(sent),
            lambda m, i: book_fair_prob(cfg, m, i, odds_cache, now, allow_fetch=False))
        site.write(ROOT / "docs" / "data.json", now, live, wallets, book,
                   len(by_cid), len(queue), cfg["consensus"]["min_wallets"])


if __name__ == "__main__":
    main()
