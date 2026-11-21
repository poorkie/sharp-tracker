"""Offline tests: grading math, signal rules, odds matching, ledger grading."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from sharp import grading, ledger, odds, signals
from sharp.markets import market_type, parse_time

LIVE_CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
# Logic tests use fixed rule settings so tuning config.yaml doesn't break them.
CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
CFG["consensus"].update({"min_wallets": 2, "min_total_usd": 2000, "alert_tiers": ["ELITE"]})
CFG["single_elite"].update({"enabled": True, "min_usd": 5000})
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
    assert "Elite avg entry + 3¢: 48¢ · ✅ at or under" in body
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


# ---------- multi-wallet detection ----------

from sharp import clusters, holders as holders_mod

CL = CFG["clusters"]


def _rows(n, start=0, flip=False, prefix="nba"):
    return [{"event_slug": f"{prefix}-a-b-2026-10-{(i % 27) + 1:02d}", "condition_id": f"m{i}",
             "token_id": f"m{i}-{'b' if flip else 'a'}"} for i in range(start, start + n)]


def _wallets(fps):
    return {w: {"grades": {"nba": {"tier": "ELITE"}}, "fp": fp} for w, fp in fps.items()}


def test_same_bets_links_wallets_but_not_strangers():
    fa = clusters.fingerprint(_rows(60), SPORTS)
    fb = clusters.fingerprint(_rows(60, start=5), SPORTS)         # 55 shared, same side
    fe = clusters.fingerprint(_rows(60, start=30), SPORTS)        # 30 shared: agree often, not same person
    fc = clusters.fingerprint(_rows(60, start=200), SPORTS)       # different bets
    fd = clusters.fingerprint(_rows(60, flip=True), SPORTS)       # same games, other side
    ent, links = clusters.build(_wallets({"a": fa, "b": fb, "c": fc, "d": fd, "e": fe}), {}, CL)
    assert ent["a"] == ent["b"] and ent["c"] == "c" and ent["d"] == "d" and ent["e"] == "e"
    assert links[0]["why"] == "same bets"


def test_same_timing_links_wallets():
    st = {}
    trades = []
    for g in range(7):
        trades += [{"proxy_wallet": "x", "condition_id": f"g{g}", "outcome_index": 0, "side": "BUY", "timestamp": 1000 * g},
                   {"proxy_wallet": "y", "condition_id": f"g{g}", "outcome_index": 0, "side": "BUY", "timestamp": 1000 * g + 30}]
    clusters.update_cotrade(st, trades, {"x", "y"}, NOW)
    clusters.update_cotrade(st, trades, {"x", "y"}, NOW)          # re-seen: not double counted
    assert st["pairs"]["x|y"] == {"both": 7, "together": 7}
    ent, _ = clusters.build(_wallets({"x": [], "y": []}), st, CL)
    assert ent["x"] == ent["y"]


def test_linked_wallets_count_as_one_person():
    g = {w: grade("ELITE") for w in ("a", "b")}
    trades = [trade("a", "BUY", 1500), trade("b", "BUY", 1500)]
    flows = signals.net_flows(trades, g, {"c1": "nhl"})
    assert signals.find_signals(flows, MKT, g, CFG, {"a": "a", "b": "a"}) == []
    s = signals.find_signals(flows, MKT, g, CFG, {"a": "a", "b": "b"})
    assert len(s) == 1 and s[0]["people"] == 2


# ---------- top holders ----------

def test_holders_add_positions_and_net_hedgers():
    g = {"a": grade("ELITE"), "b": grade("ELITE"), "h": grade("ELITE")}
    cache = {"c1": {"at": NOW.isoformat(), "rows": [
        {"w": "a", "i": 0, "s": 3000, "p": 0.42},                    # bought days ago
        {"w": "b", "i": 0, "s": 2500, "p": 0.44},
        {"w": "h", "i": 0, "s": 1000, "p": 0.45}, {"w": "h", "i": 1, "s": 1000, "p": 0.55},  # hedger
    ]}}
    flows = {}
    assert holders_mod.merge(flows, cache, MKT, g) == 2
    s = signals.find_signals(flows, MKT, g, CFG)
    assert len(s) == 1 and s[0]["kind"] == "ELITE_CONSENSUS"
    assert {w["via"] for w in s[0]["wallets"]} == {"holding"}
    assert abs(s[0]["wallets"][0]["entry"] - 0.42) < 1e-9


# ---------- 15-minute lock ----------

def _pick(price=0.46, max_prob=0.49, start_in_min=10):
    m = {**MKT["c1"], "game_start": (NOW + timedelta(minutes=start_in_min)).isoformat()}
    e = ledger.add_alert([], {**_sig("c1", 0, 3000), "price": price, "market": m}, None, NOW - timedelta(hours=3))
    e["max_prob"] = max_prob
    return e, {"c1": {**m, "prices": [price, 1 - price]}}


def test_lock_play_pass_and_locked_record(monkeypatch):
    e1, mk = _pick()
    assert ledger.lock_picks([e1], mk, {"c1:0"}, NOW) and e1["lock"]["verdict"] == "PLAY"
    e2, mk2 = _pick(price=0.46, max_prob=0.40)
    e2["book_fair"] = 0.42                                         # Poly 4c above the books
    ledger.lock_picks([e2], mk2, {"c1:0"}, NOW)
    assert e2["lock"]["verdict"] == "PASS" and "books" in e2["lock"]["reason"]
    e5, mk5 = _pick(price=0.46, max_prob=0.40)                     # moved past Elite entry, no book line
    ledger.lock_picks([e5], mk5, {"c1:0"}, NOW)
    assert e5["lock"]["verdict"] == "PLAY"
    e3, mk3 = _pick()
    ledger.lock_picks([e3], mk3, set(), NOW)
    assert e3["lock"]["verdict"] == "PASS" and "left" in e3["lock"]["reason"]
    e4, mk4 = _pick(start_in_min=60)                              # too early to lock
    assert ledger.lock_picks([e4], mk4, {"c1:0"}, NOW) == []
    for e in (e1, e2, e3):
        e["game_start"] = (NOW - timedelta(hours=5)).isoformat()
    monkeypatch.setattr(ledger.api, "markets_by_condition",
                        lambda ids, closed: [{"conditionId": "c1", "outcomePrices": '["1","0"]'}])
    ledger.grade_results([e1, e2, e3], NOW)
    k = ledger.summary_locked([e1, e2, e3])
    assert (k["n"], k["w"], k["passed"], k["passed_w"]) == (1, 1, 2, 2)
    assert abs(k["units"] - 1.17) < 0.01


def test_activity_merges_fills_and_sorts_newest_first():
    from sharp import activity
    log = {}
    g = {"a": grade("ELITE"), "b": grade("SHARP"), "z": grade("NONE")}
    t0 = int(NOW.timestamp())
    trades = [{**trade("a", "BUY", 500), "timestamp": t0 - 900},
              {**trade("a", "BUY", 700), "timestamp": t0 - 700},   # same side within 10 min: merged
              {**trade("b", "BUY", 300, idx=1, price=0.54), "timestamp": t0 - 60},
              {**trade("z", "BUY", 9000), "timestamp": t0}]          # not graded sharp: ignored
    activity.record(log, trades, {}, {"a", "b"}, NOW)
    activity.record(log, trades, {}, {"a", "b"}, NOW)              # re-seen trades aren't duplicated
    rows = activity.feed(log, MKT, g, {}, {"c1:0"})
    assert [r["wallet"] for r in rows] == ["b", "a"]
    assert abs(rows[1]["usd"] - 1200) < 1e-6 and rows[1]["is_pick"] and rows[1]["pick"] == "Rangers"


def test_live_rules_need_three_elite_people():
    g = {w: grade("ELITE") for w in ("a", "b", "c")}
    two = [trade("a", "BUY", 1500), trade("b", "BUY", 1500)]
    flows = signals.net_flows(two, g, {"c1": "nhl"})
    assert signals.find_signals(flows, MKT, g, LIVE_CFG) == []
    flows = signals.net_flows(two + [trade("c", "BUY", 1000)], g, {"c1": "nhl"})
    s = signals.find_signals(flows, MKT, g, LIVE_CFG)
    assert len(s) == 1 and s[0]["people"] == 3
    # three wallets that are one person don't count
    assert signals.find_signals(flows, MKT, g, LIVE_CFG, {"a": "a", "b": "a", "c": "a"}) == []


# ---------- in-game paper trading ----------

from sharp import ingame


def _live(start_min_ago=40):
    return {"c1": {**MKT["c1"], "game_start": (NOW - timedelta(minutes=start_min_ago)).isoformat(),
                   "tokens": ["tokA", "tokB"]}}


def _t(w, side, usd, idx=0, price=0.5, min_ago=3):
    return {"proxy_wallet": w, "condition_id": "c1", "outcome_index": idx, "side": side,
            "size": usd / price, "price": price, "timestamp": int(NOW.timestamp()) - 60 * min_ago}


def test_ingame_detects_elite_live_bets_only():
    g = {"e": grade("ELITE"), "h": grade("ELITE"), "s": grade("SHARP")}
    live = _live()
    trades = [_t("e", "BUY", 300, min_ago=4), _t("e", "BUY", 400, min_ago=3),   # merged: $700
              _t("s", "BUY", 5000),                                              # not Elite
              _t("e", "BUY", 900, min_ago=60),                                   # pre-game
              _t("h", "BUY", 3000, idx=1, min_ago=8), _t("h", "BUY", 500, idx=0, min_ago=2)]  # hedge
    sigs = ingame.detect(trades, live, g, {}, [], NOW, LIVE_CFG)
    assert [(x["w"], x["idx"]) for x in sigs] == [("h", 1), ("e", 0)]
    e = next(x for x in sigs if x["w"] == "e")
    assert abs(e["usd"] - 700) < 1e-6
    old = [_t("e", "BUY", 500, min_ago=30)]                                      # too stale to follow
    assert ingame.detect(old, _live(90), g, {}, [], NOW, LIVE_CFG) == []


def test_ingame_record_one_pick_per_side_and_grading(monkeypatch):
    g = {"e": grade("ELITE"), "f": grade("ELITE"), "x": grade("ELITE")}
    live = _live()
    book = []
    sigs = ingame.detect([_t("e", "BUY", 600), _t("f", "BUY", 800, min_ago=2)], live, g, {}, book, NOW, LIVE_CFG)
    new = ingame.record(book, sigs, live, NOW, LIVE_CFG, ask=lambda tok: (0.52, 400))
    assert len(new) == 1 and len(book) == 1 and book[0]["elite_wallets"] == ["e", "f"]
    assert book[0]["fill_price"] == 0.52 and book[0]["delay_s"] == 180
    other = ingame.detect([_t("x", "BUY", 900, idx=1)], live, g, {}, book, NOW, LIVE_CFG)
    assert ingame.record(book, other, live, NOW, LIVE_CFG, ask=lambda tok: (0.48, 400)) == []  # conflict
    far = ingame.detect([_t("x", "BUY", 900)], {"c1": {**live["c1"], "condition_id": "c1"}}, g, {}, [], NOW, LIVE_CFG)
    assert ingame.record([], far, live, NOW, LIVE_CFG, ask=lambda tok: (0.98, 400)) == []    # out of band
    monkeypatch.setattr(ingame.api, "markets_by_condition",
                        lambda ids, closed: [{"conditionId": "c1", "outcomePrices": '["1","0"]'}])
    assert ingame.grade(book, NOW + timedelta(hours=2)) == 1
    assert book[0]["result"] == "WIN" and abs(book[0]["pnl_100"] - 92.31) < 0.01
    assert ingame.summary(book)["units"] == 0.92


def test_map_winner_label():
    from sharp.markets import pick_label
    m = {"question": "LoL: T1 vs Gen.G - Game 2 Winner", "market_type": "child_moneyline", "outcomes": ["T1", "Gen.G"]}
    assert pick_label(m, 0) == "T1 (Map 2)"


def test_ingame_keeps_latest_separate_bet():
    g = {"e": grade("ELITE")}
    trades = [_t("e", "BUY", 300, min_ago=12), _t("e", "BUY", 500, min_ago=3)]   # 9 min apart
    sigs = ingame.detect(trades, _live(), g, {}, [], NOW, LIVE_CFG)
    assert len(sigs) == 1 and abs(sigs[0]["usd"] - 500) < 1e-6


# ---------- Novig ----------

import base64 as _b64
import json as _json

from sharp import novig as nv


def test_novig_signer_matches_all_official_vectors():
    V = _json.loads((Path(__file__).parent.parent / "vendor-docs/novig/signing-vectors.json").read_text())
    for v in V["vectors"]:
        i, b = v["input"], v["input"]["body"]
        body = b.encode() if isinstance(b, str) else _json.dumps(b, separators=(",", ":")).encode()
        s = nv.canonical_string(str(i["timestamp"]), i["method"], i["path"], i["query"], body)
        assert s == v["string_to_sign"], v["id"]
        if v["algorithm"] == "ed25519":
            k = nv.Key.from_pem("x", V["keypairs"][v["keypair_id"]]["private_key_pkcs8_pem"])
            assert _b64.b64encode(k.private.sign(s.encode())).decode() == v["signature"], v["id"]


def test_novig_pricing_helpers():
    book = {"orders": {"A": [{"price": "0.47", "qty": 900}], "B": [{"price": "0.51", "qty": 5000}]}}
    assert nv.best_ask(book, "A") == (0.49, 5000)       # buy A = cross best bid on B
    assert nv.on_grid(0.4912) == "0.495" and nv.on_grid(0.0412) == "0.042" and nv.on_grid(0.9612) == "0.962"
    assert nv.Key.from_seed("k", "same").public_pem() == nv.Key.from_seed("k", "same").public_pem()


class _FakeNovig:
    def __init__(self, fills):
        self.placed, self._fills = [], fills
    def live_money_markets(self, league):
        return [{"marketId": "m1", "startsTs": int(NOW.timestamp() * 1000) - 2400_000,
                 "outcomes": [{"outcomeId": "oR", "name": "New York Rangers"},
                              {"outcomeId": "oB", "name": "Boston Bruins"}]}]
    def book(self, mid):
        return {"orders": {"oB": [{"price": "0.48", "qty": 30000}]}}
    def place(self, outcome, price, qty, tif):
        self.placed.append((outcome, price, qty, tif)); return {"orderId": "ord1"}
    def fills_for_order(self, oid):
        return self._fills


def test_copy_to_novig_and_grade(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    live = _live()
    e = {"idx": 0, "fill_price": 0.50, "result": None}
    fake = _FakeNovig([{"qty": 19231, "cost": "100.00", "fee": "0.75"}])
    rec = ingame.copy_to_novig(e, live["c1"], fake, {}, LIVE_CFG)
    assert fake.placed == [("oR", "0.520", 19231, "IOC")]
    assert rec["status"] == "filled" and rec["avg_price"] == 0.52 and rec["fee"] == 0.75
    e.update(novig=rec, cid="c1", alerted_at=(NOW - timedelta(hours=2)).isoformat())
    monkeypatch.setattr(ingame.api, "markets_by_condition",
                        lambda ids, closed: [{"conditionId": "c1", "outcomePrices": '["1","0"]'}])
    ingame.grade([e], NOW)
    assert abs(e["novig"]["pnl"] - (192.31 - 100 - 0.75)) < 0.01
    m2 = {**live["c1"], "market_type": "child_moneyline"}
    assert ingame.copy_to_novig(e, m2, fake, {}, LIVE_CFG)["status"] == "skipped"


def test_merge_live_replaces_slow_scan_pick():
    from sharp import ingame
    scan = {"id": "a-0-1", "cid": "0xa", "idx": 0, "elite_wallets": ["w1"], "fill_price": 0.70, "sport": "nba", "alerted_at": "2026-11-21T00:00:00+00:00",
            "elite_price": 0.60, "delay_s": 200, "result": None, "pnl_100": None}
    other = {"id": "b-1-1", "cid": "0xb", "idx": 1, "elite_wallets": ["w2"], "fill_price": 0.5, "sport": "nba", "alerted_at": "2026-11-21T00:00:00+00:00",
             "elite_price": 0.5, "delay_s": 100, "result": None, "pnl_100": None, "source": "live"}
    book = [scan, other]
    live = [{"id": "a-0-9", "cid": "0xa", "idx": 0, "elite_wallets": ["w3"], "fill_price": 0.61, "sport": "nba", "alerted_at": "2026-11-21T00:00:00+00:00",
             "elite_price": 0.60, "delay_s": 6, "result": None, "pnl_100": None, "source": "live"},
            {"id": "b-0-9", "cid": "0xb", "idx": 0, "elite_wallets": ["w4"], "fill_price": 0.5, "sport": "nba", "alerted_at": "2026-11-21T00:00:00+00:00",
             "elite_price": 0.5, "delay_s": 5, "result": None, "pnl_100": None, "source": "live"},
            dict(other)]
    assert ingame.merge_live(book, live) == 1
    a = [e for e in book if e["cid"] == "0xa"]
    assert len(a) == 1 and a[0]["fill_price"] == 0.61 and set(a[0]["elite_wallets"]) == {"w1", "w3"}
    assert a[0]["replaced_scan_fill"] == 0.70
    assert len([e for e in book if e["cid"] == "0xb"]) == 1      # other side not added
    assert ingame.merge_live(book, live) == 0                     # idempotent
    s = ingame.site_data(book)["by_source"]
    assert s["live"]["pending"] == 2 and s["scan"]["pending"] == 0
