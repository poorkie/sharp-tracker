"""Offline tests: grading math, signal rules, odds matching, ledger grading."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from sharp import grading, ledger, odds, signals
from sharp.markets import market_type, parse_time

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
SPORTS = CFG["sports"]
G = CFG["grading"]
NOW = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)


def pos(slug, avg, size, pnl, status="CLOSED", cid=None, tok="t1"):
    return {"event_slug": slug, "avg_price": avg, "total_size": size,
            "realized_pnl": pnl if status == "CLOSED" else 0,
            "total_pnl": pnl, "status": status,
            "condition_id": cid or f"0x{slug}{avg}{size}{pnl}", "token_id": tok}


# ---------- grading ----------

def test_unredeemed_losers_are_counted():
    rows = [pos("nhl-a-b-2026-10-01", 0.5, 200, 100)] * 1  # one win
    rows.append(pos("nhl-c-d-2026-10-02", 0.5, 200, -100, status="REDEEMABLE"))
    bets = grading.bets_from_positions(rows, SPORTS, True)
    assert len(bets) == 2 and sum(b.pnl for b in bets) == 0


def test_futures_excluded_when_games_only():
    rows = [pos("nhl-stanley-cup-champion", 0.1, 1000, 500)]
    assert grading.bets_from_positions(rows, SPORTS, True) == []
    assert len(grading.bets_from_positions(rows, SPORTS, False)) == 1


def test_unknown_sport_ignored():
    assert grading.bets_from_positions([pos("presidential-2028-11-07", .5, 10, 5)], SPORTS, True) == []


def test_hot_streak_is_not_sharp():
    rows = [pos(f"nba-x-y-2026-10-{i:02d}", 0.5, 100, 50) for i in range(1, 11)]  # 10-0
    g = grading.grade_wallet(rows, SPORTS, G)["nba"]
    assert g["n"] == 10 and g["roi"] == 1.0 and g["tier"] == "NONE"


def test_long_profitable_record_is_sharp():
    rows = []
    for i in range(120):  # buys at 0.48, wins 58% -> real edge
        win = i % 50 < 29
        rows.append(pos(f"nhl-a-b-2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}", 0.48, 200,
                        104 if win else -96, cid=f"c{i}"))
    g = grading.grade_wallet(rows, SPORTS, G)["nhl"]
    assert g["tier"] in ("SHARP", "ELITE"), g
    assert g["hit_edge"] > 0.05
    assert g["shrunk_roi"] < g["roi"]  # shrinkage pulls toward the prior


def test_volume_without_edge_is_not_sharp():
    rows = [pos(f"nfl-a-b-2026-10-{(i % 28) + 1:02d}", 0.5, 1000, 500 if i % 2 else -500, cid=f"c{i}")
            for i in range(300)]
    assert grading.grade_wallet(rows, SPORTS, G)["nfl"]["tier"] == "NONE"


# ---------- signals ----------

MKT = {"c1": {"condition_id": "c1", "sport": "nhl", "prices": [0.46, 0.54],
              "outcomes": ["Rangers", "Bruins"], "event_title": "Rangers vs. Bruins",
              "question": "Rangers vs. Bruins", "market_type": "moneyline",
              "game_start": (NOW + timedelta(hours=5)).isoformat(),
              "url": "https://polymarket.com/event/nhl-nyr-bos-2026-11-10",
              "event_slug": "nhl-nyr-bos-2026-11-10"}}


def grade(tier, roi=0.05):
    return {"nhl": {"tier": tier, "shrunk_roi": roi, "n": 100, "t_stat": 2.1}}


def trade(w, side, usd, idx=0, price=0.46):
    return {"proxy_wallet": w, "condition_id": "c1", "outcome_index": idx,
            "outcome": MKT["c1"]["outcomes"][idx], "side": side,
            "size": usd / price, "price": price}


def run_sig(trades, grades):
    flows = signals.net_flows(trades, grades, {"c1": "nhl"})
    return signals.find_signals(flows, MKT, grades, CFG)


def test_elite_consensus_fires():
    g = {w: grade("ELITE") for w in ("a", "b")}
    s = run_sig([trade("a", "BUY", 1200), trade("b", "BUY", 1000)], g)
    assert len(s) == 1 and s[0]["kind"] == "ELITE_CONSENSUS" and s[0]["outcome"] == "Rangers"
    assert abs(s[0]["wallets"][0]["entry"] - 0.46) < 1e-6


def test_sharp_tier_no_longer_triggers_picks():
    g = {w: grade("SHARP") for w in ("a", "b", "c", "d")}
    trades = [trade(w, "BUY", 3000) for w in ("a", "b", "c", "d")]
    assert run_sig(trades, g) == []


def test_non_elites_and_scalpers_ignored():
    g = {"a": grade("ELITE"), "b": grade("ELITE"), "d": grade("NONE")}
    trades = [trade("a", "BUY", 1500), trade("b", "BUY", 1500), trade("b", "SELL", 1500),  # b scalped out
              trade("d", "BUY", 50000)]                                                   # big, not graded
    assert run_sig(trades, g) == []


def test_sharp_money_on_other_side_still_blocks():
    g = {"a": grade("ELITE"), "b": grade("ELITE"), "x": grade("SHARP")}
    trades = [trade("a", "BUY", 1500), trade("b", "BUY", 1500),
              trade("x", "BUY", 2000, idx=1, price=0.54)]
    assert run_sig(trades, g) == []


def test_single_elite():
    s = run_sig([trade("e", "BUY", 6000)], {"e": grade("ELITE", 0.1)})
    assert len(s) == 1 and s[0]["kind"] == "ELITE"


def test_dedupe():
    sig = {"condition_id": "c1", "outcome_index": 0, "sharp_usd": 3000}
    sent = {}
    assert signals.should_alert(sig, sent, 0.5)
    sent["c1:0"] = 3000
    assert not signals.should_alert({**sig, "sharp_usd": 4000}, sent, 0.5)
    assert signals.should_alert({**sig, "sharp_usd": 4600}, sent, 0.5)


# ---------- odds ----------

GAME = {"home_team": "Boston Bruins", "away_team": "New York Rangers",
        "commence_time": (NOW + timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
        "bookmakers": [
            {"title": "BookA", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Boston Bruins", "price": -130}, {"name": "New York Rangers", "price": 110}]}]},
            {"title": "BookB", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Boston Bruins", "price": -125}, {"name": "New York Rangers", "price": 105}]}]},
        ]}


def test_american_conversions():
    assert abs(odds.american_to_prob(-110) - 0.5238) < 1e-3
    assert abs(odds.american_to_prob(150) - 0.4) < 1e-9
    assert odds.prob_to_american(0.4) == "+150"
    assert odds.prob_to_american(0.6) == "-150"


def test_fair_prob_matching():
    p, books = odds.find_fair_prob([GAME], "Rangers", NOW + timedelta(hours=5), "Bruins")
    assert books == 2 and 0.44 < p < 0.47
    assert odds.find_fair_prob([GAME], "Rangers", NOW + timedelta(days=3)) is None
    assert odds.find_fair_prob([GAME], "Islanders", NOW + timedelta(hours=5)) is None


# ---------- markets ----------

def test_parse_time_formats():
    assert parse_time("2026-11-10 00:00:00+00") == datetime(2026, 11, 10, tzinfo=timezone.utc)
    assert parse_time("2026-11-10T23:00:00Z").hour == 23


def test_market_type_fallback():
    assert market_type({"sportsMarketType": "moneyline"}) == "moneyline"
    assert market_type({"question": "Rangers vs. Bruins: O/U 6.5", "outcomes": '["Over","Under"]'}) == "totals"
    assert market_type({"question": "Spread: Rangers (-1.5)"}) == "spreads"
    assert market_type({"question": "Rangers vs. Bruins", "outcomes": '["Rangers","Bruins"]'}) == "moneyline"


# ---------- ledger ----------

def test_ledger_grading(monkeypatch):
    book = []
    sig = {"condition_id": "c1", "outcome_index": 0, "outcome": "Rangers", "kind": "CONSENSUS",
           "price": 0.46, "sharp_usd": 2700, "wallets": [1, 2, 3], "market": MKT["c1"]}
    ledger.add_alert(book, sig, 0.455, NOW)
    # price moves before the game -> closing price updates
    moved = {"c1": {**MKT["c1"], "prices": [0.51, 0.49]}}
    ledger.update_closing_prices(book, moved, NOW + timedelta(hours=1))
    assert book[0]["close_price"] == 0.51
    monkeypatch.setattr(ledger.api, "markets_by_condition",
                        lambda ids, closed: [{"conditionId": "c1", "outcomePrices": '["1","0"]'}])
    assert ledger.grade_results(book, NOW + timedelta(hours=10)) == 1
    e = book[0]
    assert e["result"] == "WIN" and abs(e["pnl_100"] - 117.39) < 0.01 and e["clv"] == 0.05
    md = ledger.render_record(book, NOW)
    assert "1-0" in md and "+1.17u" in md


def test_alert_format():
    import run
    sig = {"kind": "CONSENSUS", "price": 0.46, "outcome": "Rangers", "sharp_usd": 2700,
           "opposing_usd": 0, "market": MKT["c1"],
           "wallets": [{"wallet": "0xabcdef1234", "tier": "SHARP", "shrunk_roi": 0.05,
                        "n": 140, "usd": 1000, "entry": 0.45}]}
    view = {"fair": 0.49, "lines": odds.book_lines(GAME, "New York Rangers")}
    max_prob = run.worth_up_to(sig, 0.03)
    assert max_prob == 0.48
    title, body = run.format_alert(sig, view, ZoneInfo("America/Chicago"), max_prob)
    assert title == "SHARP CONSENSUS · NHL"
    assert "Rangers @ 46¢ (+117)" in body and "Poly cheaper by 3.0¢" in body
    assert "Worth it up to +108 (48¢) · Polymarket ✅ OK" in body
    assert "Best book: BookA +110 ✅" in body and "Also: BookB +105" in body
    assert "CST" in body


def test_book_lines_best_first():
    lines = odds.book_lines(GAME, "Boston Bruins")
    assert [l["price"] for l in lines] == [-125, -130]


def _sig(cid, idx, usd, outcome="Knicks"):
    return {"condition_id": cid, "outcome_index": idx, "outcome": outcome, "kind": "CONSENSUS",
            "price": 0.6, "sharp_usd": usd, "wallets": [1, 2, 3], "market": MKT["c1"]}


def test_realert_updates_not_duplicates():
    book = []
    ledger.record_alert(book, _sig("c1", 0, 3000), None, NOW)
    ledger.record_alert(book, {**_sig("c1", 0, 9000), "price": 0.7}, None, NOW + timedelta(hours=1))
    assert len(book) == 1 and book[0]["entry_price"] == 0.6 and book[0]["sharp_usd"] == 9000
    assert book[0]["updates"] == 1


def test_other_side_blocked_and_dedupe_repairs_old_ledgers():
    book = []
    ledger.record_alert(book, _sig("c1", 0, 3000), None, NOW)
    assert ledger.has_other_side(book, _sig("c1", 1, 5000))
    old = []
    for i, (cid, idx) in enumerate([("c1", 0), ("c1", 0), ("c1", 1), ("c2", 0), ("c1", 0)]):
        old.append(ledger.add_alert([], _sig(cid, idx, 1000 * (i + 1)), None, NOW + timedelta(minutes=i)))
    fixed = ledger.dedupe(old)
    assert [(e["condition_id"], e["outcome_index"]) for e in fixed] == [("c1", 0), ("c2", 0)]
    assert fixed[0]["sharp_usd"] == 5000 and fixed[0]["entry_price"] == 0.6


def test_pick_labels():
    from sharp.markets import pick_label
    sp = {"question": "Spread: Knicks (-4.5)", "market_type": "spreads", "outcomes": ["Knicks", "Cavaliers"]}
    tot = {"question": "Knicks vs. Cavaliers: O/U 221.5", "market_type": "totals", "outcomes": ["Over", "Under"]}
    assert pick_label(sp, 0) == "Knicks -4.5" and pick_label(sp, 1) == "Cavaliers +4.5"
    assert pick_label(tot, 0) == "Over 221.5"


def test_one_line_per_game_and_type():
    book = []
    a = {**_sig("c1", 0, 3000), "market": {**MKT["c1"], "market_type": "spreads"}}
    b = {**_sig("c9", 0, 2500), "market": {**MKT["c1"], "market_type": "spreads"}}
    c = {**_sig("c8", 0, 2500), "market": {**MKT["c1"], "market_type": "totals"}}
    ledger.record_alert(book, a, None, NOW)
    assert ledger.has_same_game_line(book, b)        # alt spread on same game: blocked
    assert not ledger.has_same_game_line(book, c)    # a total on same game: allowed
    assert not ledger.has_same_game_line(book, a)    # same market: handled as an update
