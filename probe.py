"""One-off #2: find game/map winner markets on Kalshi and Polymarket US. Writes PROBE.md."""
import json, re, traceback, inspect, requests
out = []
def p(*a): out.append(" ".join(str(x) for x in a))
K = "https://api.elections.kalshi.com/trade-api/v2"
try:
    ser = requests.get(f"{K}/series", params={"category": "Sports"}, timeout=30).json().get("series") or []
    want = [x for x in ser if (x.get("tags") or [None])[0] in ("Esports", "Basketball", "Hockey", "Tennis")
            and re.search(r"GAME|MAP|MATCH", x["ticker"]) and not re.search(r"TOTAL|SPREAD|PLAYOFF|SERIES|1H|2H|QTOTAL|PROP", x["ticker"])]
    p("## Kalshi candidate series", len(want))
    for x in want:
        m = requests.get(f"{K}/markets", params={"series_ticker": x["ticker"], "status": "open", "limit": 200}, timeout=30).json().get("markets") or []
        p(f"- {x['ticker']} | {x['title']} | {x.get('tags')} | open markets: {len(m)}")
        if m:
            s = m[0]
            p("   e.g.", json.dumps({k: s.get(k) for k in ("ticker", "event_ticker", "title", "yes_sub_title", "yes_ask_dollars", "no_ask_dollars", "expected_expiration_time", "occurrence_datetime", "close_time")}))
    ev = requests.get(f"{K}/events", params={"series_ticker": "KXLOLGAMES", "status": "open", "limit": 2, "with_nested_markets": "true"}, timeout=30).json()
    p("events sample:", json.dumps(ev)[:1500])
except Exception:
    p("Kalshi error", traceback.format_exc())

G = "https://gateway.polymarket.us"
p("\n## Polymarket US")
try:
    sp = requests.get(f"{G}/v1/sports", timeout=30).json()
    p("sports:", [(s.get("sport"), s.get("series"), s.get("tags")) for s in sp.get("sports", [])])
    import polymarket_us, pathlib
    root = pathlib.Path(polymarket_us.__file__).parent
    for f in sorted(root.rglob("*.py")):
        txt = f.read_text()
        for line in txt.splitlines():
            if re.search(r'["\'](/v1/[^"\']+)["\']', line) or "f\"/v1" in line or "f'/v1" in line:
                p("  sdk", f.name, "|", line.strip()[:160])
except Exception:
    p("PMUS error", traceback.format_exc())
try:
    r = requests.get(f"{G}/v1/events", params={"active": "true", "closed": "false", "limit": 50}, timeout=30).json()
    evs = r.get("events", [])
    p("active events:", len(evs), "keys:", list(evs[0].keys()) if evs else None)
    for e in evs[:50]:
        p("  ev", e.get("slug"), "|", e.get("title"), "|", e.get("seriesSlug"), "|", e.get("startTime"), "| live:", e.get("live"), "| markets:", len(e.get("markets") or []))
    if evs:
        p("event full:", json.dumps(evs[0])[:2500])
    r = requests.get(f"{G}/v1/markets", params={"active": "true", "closed": "false", "limit": 5}, timeout=30).json()
    mk = (r.get("markets") or [])
    if mk:
        p("market full:", json.dumps(mk[0])[:2500])
        slug = mk[0]["slug"]
        for path in (f"/v1/markets/{slug}/book", f"/v1/markets/{slug}/bbo", f"/v1/markets/{slug}", f"/v1/book/{slug}", f"/v1/bbo/{slug}", f"/v1/markets/book/{slug}", f"/v1/markets/bbo/{slug}"):
            rr = requests.get(G + path, timeout=20)
            p(f"GET {path} -> {rr.status_code}: {rr.text[:500]}")
except Exception:
    p("PMUS error", traceback.format_exc())
open("PROBE.md", "w").write("\n".join(out))
