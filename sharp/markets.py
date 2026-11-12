"""Find upcoming single-game markets on Polymarket."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from . import api

log = logging.getLogger(__name__)
DATE_IN_SLUG = re.compile(r"(\d{4}-\d{2}-\d{2})")


def parse_time(v) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    s = v.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    if re.search(r"[+-]\d{2}$", s):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def market_type(m: dict) -> str:
    t = (m.get("sportsMarketType") or "").lower()
    if t:
        return t
    q = (m.get("question") or "").lower()
    outs = [o.lower() for o in api.parse_json_list(m.get("outcomes"))]
    if "over" in outs or "o/u" in q or "total" in q:
        return "totals"
    if "spread" in q or re.search(r"\([+-]\d", q):
        return "spreads"
    return "moneyline"


def game_start(m: dict, ev: dict) -> datetime | None:
    for v in (m.get("gameStartTime"), ev.get("startTime"), ev.get("gameStartTime"),
              m.get("eventStartTime")):
        dt = parse_time(v)
        if dt:
            return dt
    mt = DATE_IN_SLUG.search(ev.get("slug") or "")
    return parse_time(mt.group(1) + "T23:00:00+00:00") if mt else None


LINE = re.compile(r"([+-]?\d+(?:\.\d+)?)")


def pick_label(m: dict, idx: int, outcome: str | None = None) -> str:
    """Readable pick: 'Knicks', 'Knicks -4.5', 'Over 221.5'."""
    outs = m.get("outcomes") or []
    name = outcome or (outs[idx] if idx < len(outs) else "")
    q = m.get("question") or ""
    mt = m.get("market_type")
    if mt == "totals":
        ln = re.search(r"O/U\s*(\d+(?:\.\d+)?)", q, re.I) or LINE.search(q.split(":")[-1])
        return f"{name} {ln.group(1)}" if ln else name
    if mt == "spreads":
        mm = re.search(r"([^:(]+?)\s*\(([+-]?\d+(?:\.\d+)?)\)", q)
        if mm:
            team, line = mm.group(1).strip(), float(mm.group(2))
            if team.lower() not in name.lower() and name.lower() not in team.lower():
                line = -line
            return f"{name} {line:+g}"
    return name


def upcoming_markets(cfg: dict, now: datetime) -> list[dict]:
    horizon = now + timedelta(hours=cfg["lookahead_hours"])
    wanted = set(cfg["market_types"])
    out: list[dict] = []
    by_tag: dict[str, list] = {}
    for sport, scfg in cfg["sports"].items():
        tag = (scfg or {}).get("tag", sport)
        if tag not in by_tag:
            try:
                by_tag[tag] = api.events_by_tag(tag)
            except Exception as e:  # one league failing shouldn't kill the run
                log.error("events for %s failed: %s", tag, e)
                by_tag[tag] = []
        events = by_tag[tag]
        before = len(out)
        for ev in events:
            slug = ev.get("slug") or ""
            if not slug.startswith(sport + "-") or not DATE_IN_SLUG.search(slug):
                continue
            for m in ev.get("markets") or []:
                if m.get("closed") or not m.get("conditionId"):
                    continue
                if market_type(m) not in wanted:
                    continue
                start = game_start(m, ev)
                if not start or not (now < start <= horizon):
                    continue
                out.append({
                    "condition_id": m["conditionId"],
                    "question": m.get("question") or ev.get("title"),
                    "event_title": ev.get("title") or m.get("question"),
                    "event_slug": slug,
                    "sport": sport,
                    "market_type": market_type(m),
                    "outcomes": api.parse_json_list(m.get("outcomes")),
                    "prices": [float(p) for p in api.parse_json_list(m.get("outcomePrices"))],
                    "game_start": start.isoformat(),
                    "url": f"https://polymarket.com/event/{slug}",
                })
        log.info("%s: %d events, %d upcoming game markets", sport, len(events), len(out) - before)
    log.info("%d upcoming markets", len(out))
    return out
