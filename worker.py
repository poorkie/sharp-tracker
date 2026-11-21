"""Real-time in-game watcher (runs 24/7 on Railway, ~$5/month).

The 10-minute GitHub scan logs in-game picks about 3 minutes after the
Elite bet, and in-game prices move a lot in 3 minutes (live picks paid
3.9c more than the Elite on average). This worker checks live games every
few seconds, so picks are logged at the price you could buy at seconds
after the Elite bet.

Each loop (every POLL_SECONDS):
  - pull trades on games in progress (same Data API the scan uses)
  - find Elite in-game BUYs with the same rules as the scan (ingame.detect)
  - log a paper pick at the best ask right now (ingame.record), marked
    source="live", send a push alert if NTFY_TOPIC is set, and save the
    pick to state/live_picks.json in the GitHub repo. The next scan merges
    it into the in-game record.
Every REFRESH_MINUTES it reloads graded wallets, existing picks and the
list of live games.

Environment:
  GITHUB_TOKEN   fine-grained token, Contents read/write on the repo
  GITHUB_REPO    e.g. poorkie/sharp-tracker
  NTFY_TOPIC     optional, for phone alerts
  POLL_SECONDS   default 8
  DRY_RUN=1      log picks but don't write to GitHub
  RUN_MINUTES    stop after N minutes (for testing); default runs forever
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from sharp import api, ingame, markets, notify

ROOT = Path(__file__).parent
log = logging.getLogger("worker")

REPO = os.environ.get("GITHUB_REPO", "poorkie/sharp-tracker")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
POLL = int(os.environ.get("POLL_SECONDS", "8"))
REFRESH_MIN = int(os.environ.get("REFRESH_MINUTES", "10"))
DRY = os.environ.get("DRY_RUN") == "1"
RUN_MIN = float(os.environ.get("RUN_MINUTES", "0"))
LIVE_FILE = "state/live_picks.json"
KEEP = 1000


# ---------------- GitHub storage ----------------

def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "sharp-worker"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def gh_read(path: str, default):
    """Read a JSON file from the repo. Returns (data, sha)."""
    r = requests.get(f"https://api.github.com/repos/{REPO}/contents/{path}",
                     headers=gh_headers(), timeout=30)
    if r.status_code == 404:
        return default, None
    r.raise_for_status()
    body = r.json()
    if body.get("content") and body.get("encoding") == "base64":
        return json.loads(base64.b64decode(body["content"])), body["sha"]
    # files over 1 MB: fetch the raw bytes
    raw = requests.get(body["download_url"], timeout=60)
    raw.raise_for_status()
    return raw.json(), body["sha"]


def gh_raw(path: str, default):
    """Big public files: raw download (no size limit, no API quota)."""
    r = requests.get(f"https://raw.githubusercontent.com/{REPO}/main/{path}", timeout=60)
    if r.status_code == 404:
        return default
    r.raise_for_status()
    return r.json()


def gh_write(path: str, data, sha: str | None, msg: str) -> str | None:
    body = {"message": msg, "content": base64.b64encode(
        json.dumps(data, indent=1, sort_keys=True).encode()).decode()}
    if sha:
        body["sha"] = sha
    r = requests.put(f"https://api.github.com/repos/{REPO}/contents/{path}",
                     headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()
    return r.json()["content"]["sha"]


def save_live(new: list[dict]) -> None:
    """Append new picks to state/live_picks.json (retry on a race)."""
    if DRY:
        log.info("DRY_RUN: not saving %d picks", len(new))
        return
    for attempt in range(4):
        try:
            cur, sha = gh_read(LIVE_FILE, [])
            ids = {e["id"] for e in cur}
            cur.extend(e for e in new if e["id"] not in ids)
            gh_write(LIVE_FILE, cur[-KEEP:], sha, f"live pick {new[-1]['pick']}")
            return
        except requests.HTTPError as e:
            log.warning("save failed (%s), retry %d", e, attempt + 1)
            time.sleep(2 + attempt * 3)
    log.error("could not save %d live picks", len(new))


# ---------------- main loop ----------------

class State:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.grades: dict = {}
        self.live: dict = {}
        self.book: list = []          # scan picks + live picks, for conflict checks
        self.loaded_at = 0.0

    def refresh(self) -> None:
        now = datetime.now(timezone.utc)
        wallets = gh_raw("state/wallets.json", {})
        self.grades = {w: v["grades"] for w, v in wallets.items()}
        scan_book = gh_raw("state/ingame.json", [])
        live_book, _ = gh_read(LIVE_FILE, [])
        seen = {(e["cid"], e["idx"]) for e in self.book}
        merged = {e["id"]: e for e in scan_book + live_book}
        # keep picks made since the last refresh that GitHub doesn't show yet
        for e in self.book:
            merged.setdefault(e["id"], e)
        self.book = list(merged.values())
        live_list: list = []
        markets.upcoming_markets(self.cfg, now, live_list)
        self.live = {m["condition_id"]: m for m in live_list}
        self.loaded_at = time.time()
        n_elite = sum(any(g.get("tier") == "ELITE" for g in v.values()) for v in self.grades.values())
        log.info("refreshed: %d live markets, %d Elite wallets, %d picks known (%d before)",
                 len(self.live), n_elite, len(self.book), len(seen))


def loop_once(st: State, channels: list[str]) -> list[dict]:
    cfg = st.cfg
    ig = cfg["ingame"]
    now = datetime.now(timezone.utc)
    if not st.live:
        return []
    since = int((now - timedelta(minutes=ig["max_delay_minutes"] + 5)).timestamp())
    trades = api.trades_for_conditions(list(st.live), ig["min_usd"] / 4, since, max_rows=5000)
    sigs = ingame.detect(trades, st.live, st.grades, {}, st.book, now, cfg)
    # only act on fresh bets: older ones were (or will be) handled by the scan
    fresh = [s for s in sigs if int(now.timestamp()) - s["ts_last"] <= ig.get("live_max_delay_s", 120)]
    new = ingame.record(st.book, fresh, st.live, now, cfg)
    for e in new:
        e["source"] = "live"
        log.info("PICK %s %s @ %.2f (Elite %.2f, %ds after)", e["sport"], e["pick"],
                 e["fill_price"], e["elite_price"], e["delay_s"])
        if channels:
            title, body = ingame.format_alert(e)
            notify.send(channels, title, body, e["url"])
    if new:
        save_live(new)
    return new


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    channels = ["ntfy"] if os.environ.get("NTFY_TOPIC") else []
    if not TOKEN and not DRY:
        log.warning("GITHUB_TOKEN not set: picks can't be saved (set DRY_RUN=1 to silence)")
    st = State(cfg)
    started = time.time()
    total = 0
    while True:
        try:
            if time.time() - st.loaded_at > REFRESH_MIN * 60:
                st.refresh()
            t0 = time.time()
            total += len(loop_once(st, channels))
            took = time.time() - t0
        except Exception as e:
            log.exception("loop error: %s", e)
            took = 0
            time.sleep(20)
        if RUN_MIN and time.time() - started > RUN_MIN * 60:
            log.info("RUN_MINUTES reached; %d picks this run", total)
            return
        time.sleep(max(1.0, POLL - took))


if __name__ == "__main__":
    main()
