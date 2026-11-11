"""Thin HTTP layer for Polymarket (Gamma + Data API v2) and The Odds API.

Everything that touches the network lives here, so the rest of the
code can be tested with fake data.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com/v2"
ODDS = "https://api.the-odds-api.com/v4"

_session = requests.Session()
_session.headers["User-Agent"] = "sharp-tracker/1.0"


def get_json(url: str, params: dict | None = None, tries: int = 4) -> Any:
    """GET with retries. Honors 429 Retry-After."""
    delay = 2.0
    for attempt in range(1, tries + 1):
        try:
            r = _session.get(url, params=params, timeout=30)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", delay))
                log.warning("429 from %s, waiting %.0fs", url, wait)
                time.sleep(min(wait, 60))
                continue
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == tries:
                raise
            log.warning("GET %s failed (%s), retry %d", url, e, attempt)
            time.sleep(delay)
            delay *= 2
    return None


# ---------------- Polymarket Data API v2 ----------------

def _paged(path: str, params: dict, max_rows: int) -> Iterator[dict]:
    """Walk a v2 cursor-paginated route."""
    params = dict(params)
    params.setdefault("limit", min(max_rows, 1000))
    got = 0
    while True:
        body = get_json(f"{DATA}{path}", params) or {}
        rows = body.get("data") or []
        for row in rows:
            yield row
            got += 1
            if got >= max_rows:
                return
        nxt = (body.get("pagination") or {}).get("next_cursor")
        if not nxt or not rows:
            return
        params = {k: v for k, v in params.items() if k != "limit"}
        params["cursor"] = nxt


def leaderboard(category: str, period: str, limit: int) -> list[dict]:
    return list(_paged("/leaderboard", {
        "category": category, "time_period": period, "sort_by": "PNL",
    }, limit))


def positions(user: str, status: str, max_rows: int) -> list[dict]:
    """Most-recent-first positions for a wallet. Sorting by TIMESTAMP
    matters: the default for CLOSED is biggest PnL first, which would
    only show a wallet's best bets."""
    return list(_paged("/positions", {
        "user": user, "status": status, "sort_by": "TIMESTAMP",
        "sort_direction": "DESC", "filter_type": "TOKENS", "filter_amount": 0.1,
    }, max_rows))


def trades_for_conditions(condition_ids: list[str], min_usd: float,
                          since_ts: int, max_rows: int = 3000) -> list[dict]:
    """Trades on up to N markets, newest first, stopping at since_ts.
    Includes maker fills so limit-order sharps aren't missed."""
    out: list[dict] = []
    for i in range(0, len(condition_ids), 20):
        chunk = condition_ids[i:i + 20]
        for row in _paged("/trades", {
            "condition": ",".join(chunk), "taker_only": "false",
            "filter_type": "CASH", "filter_amount": min_usd,
        }, max_rows):
            if int(row.get("timestamp", 0)) < since_ts:
                break
            out.append(row)
    return out


# ---------------- Polymarket Gamma ----------------

def events_by_tag(tag_slug: str, limit: int = 100) -> list[dict]:
    out, offset = [], 0
    while True:
        page = get_json(f"{GAMMA}/events", {
            "tag_slug": tag_slug, "active": "true", "closed": "false",
            "limit": limit, "offset": offset,
        }) or []
        out.extend(page)
        if len(page) < limit or offset > 2000:
            return out
        offset += limit


def markets_by_condition(condition_ids: list[str], closed: bool) -> list[dict]:
    out = []
    for i in range(0, len(condition_ids), 20):
        params = [("condition_ids", c) for c in condition_ids[i:i + 20]]
        params.append(("closed", "true" if closed else "false"))
        params.append(("limit", "100"))
        out.extend(get_json(f"{GAMMA}/markets", params) or [])
    return out


def parse_json_list(v: Any) -> list:
    """Gamma returns some arrays as JSON strings, e.g. '["Yes","No"]'."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.startswith("["):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return []


CLOB = "https://clob.polymarket.com"


def price_history(token_id: str, start_ts: int, end_ts: int, fidelity_min: int = 5) -> list[dict]:
    """[{t: unix, p: price}, ...] for one outcome token."""
    body = get_json(f"{CLOB}/prices-history", {
        "market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity_min,
    }) or {}
    return body.get("history") or []


# ---------------- The Odds API ----------------

def sportsbook_odds(sport_key: str, api_key: str, regions: str) -> list[dict]:
    return get_json(f"{ODDS}/sports/{sport_key}/odds", {
        "apiKey": api_key, "regions": regions, "markets": "h2h",
        "oddsFormat": "american",
    }) or []
