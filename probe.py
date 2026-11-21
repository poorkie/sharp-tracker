"""One-off #3: can a polymarket.com game be found on Polymarket US by slug? Writes PROBE.md."""
import json, traceback, requests, yaml
from datetime import datetime, timezone
from sharp import markets
out = []
def p(*a): out.append(" ".join(str(x) for x in a))
G = "https://gateway.polymarket.us"
cfg = yaml.safe_load(open("config.yaml"))
now = datetime.now(timezone.utc)
live = []
up = markets.upcoming_markets(cfg, now, live)
p("live", len(live), "upcoming", len(up))
p("sample market dict keys:", list((live or up)[0].keys()))
seen = []
for m in live + up:
    if m["sport"] in ("dota2", "lol", "nba", "nhl", "cbb", "atp") and m["event_slug"] not in seen:
        seen.append(m["event_slug"])
picked = []
for sp in ("dota2", "lol", "nba", "nhl", "cbb", "atp"):
    picked += [s for s in seen if s.startswith(sp)][:3]
for slug in picked:
    for pre in ("", "aec-", "tec-"):
        r = requests.get(f"{G}/v1/events/slug/{pre}{slug}", timeout=20)
        if r.status_code == 200:
            e = r.json().get("event") or r.json()
            p(f"HIT {pre}{slug}: {e.get('title')} live={e.get('live')}")
            for mk in (e.get("markets") or [])[:6]:
                p("   mk", json.dumps({k: mk.get(k) for k in ("slug", "question", "outcomes", "outcomePrices", "sportsMarketType", "marketType", "groupItemTitle", "active", "closed")})[:400])
            break
    else:
        p(f"miss {slug}")
# also: how to list game events for a sport
for params in ({"seriesSlug": "dota2-2025"}, {"series": "64"}, {"tags": "258"}, {"tagId": "258"}, {"sport": "dota2"}, {"category": "sports", "live": "true"}):
    r = requests.get(f"{G}/v1/events", params={**params, "active": "true", "closed": "false", "limit": 5}, timeout=20)
    try:
        ev = r.json().get("events") or []
        p("list", params, r.status_code, [(x.get("slug"), x.get("live")) for x in ev])
    except Exception:
        p("list", params, r.status_code, r.text[:200])
open("PROBE.md", "w").write("\n".join(out))
