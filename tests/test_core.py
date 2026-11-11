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


def test_consensus_fires():
    g = {w: grade("SHARP") for w in ("a", "b", "c")}
    s = run_sig([trade("a", "BUY", 1000), trade("b", "BUY", 900), trade("c", "BUY", 800)], g)
    assert len(s) == 1 and s[0]["kind"] == "CONSENSUS" and s[0]["outcome"] == "Rangers"
    assert abs(s[0]["wallets"][0]["entry"] - 0.46) < 1e-6


def test_non_sharps_and_scalpers_ignored():
    g = {"a": grade("SHARP"), "b": grade("SHARP"), "c": grade("SHARP"), "d": grade("NONE")}
    trades = [trade("a", "BUY", 1000), trade("b", "BUY", 900),
              trade("c", "BUY", 800), trade("c", "SELL", 800),  # c scalped out
              trade("d", "BUY", 50000)]                          # big but not sharp
    assert run_sig(trades, g) == []


def test_sharp_opposition_blocks():
    g = {w: grade("SHARP") for w in ("a", "b", "c", "x")}
    trades = [trade("a", "BUY", 1000), trade("b", "BUY", 900), trade("c", "BUY", 800),
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
            {"markets": [{"key": "h2h", "outcomes": [
                {"name": "Boston Bruins", "price": -130}, {"name": "New York Rangers", "price": 110}]}]},
            {"markets": [{"key": "h2h", "outcomes": [
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
    title, body = run.format_alert(sig, 0.49, ZoneInfo("America/Chicago"))
    assert title == "SHARP CONSENSUS · NHL"
    assert "Rangers @ 46¢ (+117)" in body and "Poly cheaper by 3.0¢" in body
    assert "CST" in body
