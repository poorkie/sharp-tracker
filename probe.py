"""One-off #4: real-world test of sharp.venues on current games. Writes PROBE.md."""
import json, yaml
from collections import Counter
from datetime import datetime, timezone
from sharp import markets, venues
cfg = yaml.safe_load(open("config.yaml"))
now = datetime.now(timezone.utc)
live = []
up = markets.upcoming_markets(cfg, now, live)
pool = [m for m in live] + [m for m in up if m["market_type"] == "moneyline" and m["sport"] in ("dota2", "lol", "nba", "nhl", "cbb", "atp")]
# child moneyline for upcoming esports too
pool += [m for m in up if m["market_type"] == "child_moneyline"][:10]
out = [f"live {len(live)}, testing {len(pool)} markets"]
stat = Counter()
shown = Counter()
for m in pool[:70]:
    q = venues.quotes(m, 0)
    for v in ("kalshi", "pmus"):
        stat[(m["sport"], m["market_type"], v, q[v]["status"])] += 1
    key = (m["sport"], m["market_type"])
    if shown[key] < 3:
        shown[key] += 1
        out.append(f"\n{m['sport']} {m['market_type']} | {m['event_title']} | {m['question']} | pick {m['outcomes'][0]} | poly prices {m['prices']}")
        out.append("  kalshi: " + json.dumps(q["kalshi"]))
        out.append("  pmus:   " + json.dumps(q["pmus"]))
        q1 = venues.quotes(m, 1)
        out.append(f"  other side: kalshi {q1['kalshi'].get('ask')} pmus {q1['pmus'].get('ask')}")
out.append("\n## status counts")
for k, n in sorted(stat.items()):
    out.append(f"{k}: {n}")
open("PROBE.md", "w").write("\n".join(out))
