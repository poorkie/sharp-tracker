"""Sportsbook comparison: no-vig fair probabilities from US books."""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone


def american_to_prob(odds: float) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def prob_to_american(p: float) -> str:
    if p <= 0 or p >= 1:
        return "n/a"
    return f"+{round(100 * (1 - p) / p)}" if p < 0.5 else f"{round(-100 * p / (1 - p))}"


def fair_probs(game: dict) -> dict[str, float]:
    """Median no-vig win probability per team across bookmakers."""
    per_team: dict[str, list[float]] = {}
    for book in game.get("bookmakers", []):
        for mkt in book.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            outs = mkt.get("outcomes", [])
            raw = {o["name"]: american_to_prob(float(o["price"])) for o in outs}
            total = sum(raw.values())
            if total <= 0:
                continue
            for name, p in raw.items():
                per_team.setdefault(name, []).append(p / total)
    return {k: statistics.median(v) for k, v in per_team.items()}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def team_matches(poly_name: str, book_name: str) -> bool:
    """'Rangers' ~ 'New York Rangers'; 'Man City' won't match, by design
    it fails safe (no comparison) rather than guessing."""
    a, b = _norm(poly_name), _norm(book_name)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    a_last, b_last = a.split()[-1], b.split()[-1]
    return len(a_last) > 3 and a_last == b_last


def find_game(odds_games: list[dict], team: str, game_start: datetime | None,
              opponent: str | None = None) -> tuple[dict, str] | None:
    """Locate the sportsbook game for a Polymarket team. Returns (game, book's team name)."""
    for g in odds_games:
        names = [g.get("home_team", ""), g.get("away_team", "")]
        hit = next((n for n in names if team_matches(team, n)), None)
        if not hit:
            continue
        if opponent and not any(team_matches(opponent, n) for n in names if n != hit):
            continue
        if game_start and g.get("commence_time"):
            ct = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
            if abs((ct - game_start).total_seconds()) > 12 * 3600:
                continue
        return g, hit
    return None


def find_fair_prob(odds_games: list[dict], team: str, game_start: datetime | None,
                   opponent: str | None = None) -> tuple[float, int] | None:
    """Return (fair prob for `team`, number of books) or None."""
    found = find_game(odds_games, team, game_start, opponent)
    if not found:
        return None
    g, hit = found
    fp = fair_probs(g)
    return (fp[hit], len(g.get("bookmakers", []))) if hit in fp else None


def book_lines(game: dict, team: str) -> list[dict]:
    """Each book's moneyline for `team`, best (highest payout) first."""
    out = []
    for book in game.get("bookmakers", []):
        for mkt in book.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            for o in mkt.get("outcomes", []):
                if o.get("name") == team:
                    price = float(o["price"])
                    out.append({"book": book.get("title") or book.get("key"),
                                "price": int(price), "prob": round(american_to_prob(price), 4)})
    return sorted(out, key=lambda x: x["prob"])


def fmt_american(price: int | float) -> str:
    return f"+{int(price)}" if price > 0 else str(int(price))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
