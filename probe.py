"""One-off: see how Kalshi and Polymarket US list live games. Writes PROBE.md."""
import json, traceback, requests, inspect
out = []
def p(*a): out.append(" ".join(str(x) for x in a))
K = "https://api.elections.kalshi.com/trade-api/v2"
try:
    s = requests.get(f"{K}/series", params={"category": "Sports"}, timeout=30).json()
    ser = s.get("series") or []
    p("## Kalshi sports series:", len(ser))
    keys = ("dota", "league of legends", "lol", "nba", "nhl", "college basketball", "ncaa", "tennis", "atp", "wta", "esport", "map")
    hits = [x for x in ser if any(k in (x.get("title", "") + " " + x.get("ticker", "")).lower() for k in keys)]
    for x in hits[:80]:
        p("-", x.get("ticker"), "|", x.get("title"), "|", x.get("tags"))
    for x in hits[:80]:
        m = requests.get(f"{K}/markets", params={"series_ticker": x["ticker"], "status": "open", "limit": 3}, timeout=30).json()
        ms = m.get("markets") or []
        if ms:
            p(f"\n### {x['ticker']} sample market")
            p("```"); p(json.dumps({k: ms[0].get(k) for k in ("ticker", "event_ticker", "title", "yes_sub_title", "no_sub_title", "subtitle", "yes_ask_dollars", "yes_bid_dollars", "no_ask_dollars", "yes_ask_size_fp", "expected_expiration_time", "close_time", "open_time", "rules_primary", "custom_strike", "status")}, indent=1)[:1500]); p("```")
            if x is hits[0] or True:
                ob = requests.get(f"{K}/markets/{ms[0]['ticker']}/orderbook", timeout=30).json()
                p("orderbook:", json.dumps(ob)[:400])
except Exception:
    p("Kalshi error", traceback.format_exc())
p("\n## Polymarket US")
try:
    import polymarket_us as pm
    p("module attrs:", [a for a in dir(pm) if not a.startswith("_")])
    for name in dir(pm):
        obj = getattr(pm, name)
        if inspect.isclass(obj) and "client" in name.lower():
            p("class", name, inspect.signature(obj))
            for a in dir(obj):
                if not a.startswith("_"):
                    p("  .", a)
except Exception:
    p("SDK error", traceback.format_exc())
for base in ("https://api.polymarket.us", "https://gateway.polymarket.us"):
    for path in ("/v1/markets", "/v1/events", "/v1/sports", "/markets", "/v1/markets?active=true&limit=3"):
        try:
            r = requests.get(base + path, params={"limit": 3} if "?" not in path else None, timeout=20)
            p(f"GET {base}{path} -> {r.status_code}: {r.text[:700]}")
        except Exception as e:
            p(f"GET {base}{path} -> {e}")
open("PROBE.md", "w").write("\n".join(out))
