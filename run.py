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
from collections import Counter

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
    cats = cfg["leaderboard"].get("categories", ["sports"])
    last = meta.get("leaderboard_at")
    if (last and now - datetime.fromisoformat(last) < timedelta(hours=24)
            and meta.get("leaderboard_cats") == cats):
        return
    added = 0
    for cat in cats:
        for period in cfg["leaderboard"]["periods"]:
            try:
                rows = api.leaderboard(cat, period, cfg["leaderboard"]["limit"])
            except Exception as e:
                log.warning("leaderboard %s/%s failed: %s", cat, period, e)
                continue
            for r in rows:
                w = (r.get("user_id") or "").lower()
                if w and w not in wallets and w not in queue:
                    queue.append(w)
                    added += 1
    meta["leaderboard_at"] = now.isoformat()
    meta["leaderboard_cats"] = cats
    log.info("leaderboard: queued %d new wallets", added)


def grade_some(cfg, wallets, queue, now, meta=None):
    g = cfg["grading"]
    if meta is not None and meta.get("grading_version") != g.get("version", 1):
        # grading rules or leagues changed: mark every wallet for a re-grade
        for v in wallets.values():
            v["graded_at"] = "2000-01-01T00:00:00+00:00"
        meta["grading_version"] = g.get("version", 1)
        log.info("grading version changed: re-grading all %d wallets", len(wallets))
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


def book_view(cfg, m, idx, odds_cache, now, allow_fetch=True) -> dict | None:
    """Sportsbook picture for outcome `idx` of market `m`:
    {"fair": no-vig prob, "lines": [{book, price, prob}, ...] best first}.
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
    yes_no = set(o.lower() for o in outs) == {"yes", "no"}
    if len(outs) == 2 and not yes_no:
        found = odds.find_game(cached["games"], outs[idx], start, outs[1 - idx])
        if not found:
            return None
        g, name = found
        fair = odds.fair_probs(g).get(name)
        return {"fair": round(fair, 4) if fair else None, "lines": odds.book_lines(g, name)}
    mt = re.search(r"will (.+?) win", m["question"] or "", re.I)
    if mt and yes_no:
        found = odds.find_game(cached["games"], mt.group(1), start)
        if found:
            g, name = found
            fair = odds.fair_probs(g).get(name)
            if fair is None:
                return None
            if outs[idx].lower() == "yes":
                return {"fair": round(fair, 4), "lines": odds.book_lines(g, name)}
            return {"fair": round(1 - fair, 4), "lines": []}  # "No" isn't a book moneyline
    return None


def book_fair_prob(cfg, m, idx, odds_cache, now, allow_fetch=True) -> float | None:
    v = book_view(cfg, m, idx, odds_cache, now, allow_fetch)
    return v["fair"] if v else None


def worth_up_to(sig, margin: float) -> float | None:
    """Highest win-probability price still worth paying: what the Elite
    wallets paid on average, plus a small margin."""
    ws = [w for w in sig["wallets"] if w.get("entry")]
    usd = sum(w["usd"] for w in ws)
    if not usd:
        return None
    avg = sum(w["usd"] * w["entry"] for w in ws) / usd
    return round(min(avg + margin, 0.97), 4)


def format_alert(sig, view, tz, max_prob=None) -> tuple[str, str]:
    fair = view["fair"] if view else None
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
    if max_prob:
        ok = p <= max_prob
        lines.append(f"Worth it up to {odds.prob_to_american(max_prob)} ({max_prob * 100:.0f}¢) · "
                     f"Polymarket {'✅ OK' if ok else '❌ too high'}")
    if view and view.get("lines"):
        best = view["lines"][0]
        tag = "✅" if (not max_prob or best["prob"] <= max_prob) else "❌ above max"
        lines.append(f"Best book: {best['book']} {odds.fmt_american(best['price'])} {tag}")
        others = ", ".join(f"{b['book']} {odds.fmt_american(b['price'])}" for b in view["lines"][1:4])
        if others:
            lines.append(f"Also: {others}")
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
    grade_some(cfg, wallets, queue, now, meta)

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

    # strongest first, so when a game has several lines we pick the one
    # with the most Elite money behind it
    for sig in sorted(found, key=lambda x: -x["sharp_usd"]):
        if not signals.should_alert(sig, sent, cfg["realert_growth"]):
            continue
        if (cfg["sports"].get(sig["market"]["sport"]) or {}).get("alerts", True) is False:
            continue
        if ledger.has_same_game_line(book, sig):
            log.info("skip %s: already picked another line on this game", sig["outcome"])
            continue
        if ledger.has_other_side(book, sig):
            log.info("skip %s: already alerted the other side", sig["outcome"])
            continue
        view = book_view(cfg, sig["market"], sig["outcome_index"], odds_cache, now)
        fair = view["fair"] if view else None
        max_prob = worth_up_to(sig, cfg["odds"].get("worth_margin", 0.03))
        title, body = format_alert(sig, view, tz, max_prob)
        is_update = ledger.find(book, sig) is not None
        if is_update:
            title = "UPDATE · " + title
        notify.send(channels, title, body, sig["market"]["url"])
        entry = ledger.record_alert(book, sig, fair, now)
        if not entry.get("books"):
            entry["books"] = (view or {}).get("lines") or []
            entry["max_prob"] = max_prob
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
            lambda m, i: book_view(cfg, m, i, odds_cache, now, allow_fetch=False),
            cfg["odds"].get("worth_margin", 0.03))
        site.write(ROOT / "docs" / "data.json", now, live, wallets, book,
                   len(by_cid), len(queue), cfg["consensus"]["min_wallets"],
                   dict(Counter(m["sport"] for m in upcoming)))


if __name__ == "__main__":
    main()
