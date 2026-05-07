#!/usr/bin/env python3
"""
Bet result tracking for the CNO Discord bot.

Stores every auto-posted bet in SQLite, attempts auto-settlement via ESPN's
public API, and provides reporting helpers for !record / !pending / !settle.
"""

import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("cno-bot")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_TIMEOUT_S = 10  # overridden by CNO_ESPN_TIMEOUT_S env var at import time

# ---------------------------------------------------------------------------
# Sport / league routing
# ---------------------------------------------------------------------------

# Maps CNO sport_league prefix → (ESPN sport, ESPN league)
# None means "not supported — flag needs_manual"
SPORT_LEAGUE_MAP = {
    "NBA":    ("basketball", "nba"),
    "NFL":    ("football",   "nfl"),
    "MLB":    ("baseball",   "mlb"),
    "NHL":    ("icehockey",  "nhl"),
    "NCAAB":  ("basketball", "mens-college-basketball"),
    "NCAAF":  ("football",   "college-football"),
    "WNBA":   ("basketball", "wnba"),
    "MLS":    ("soccer",     "usa.1"),
    "EPL":    ("soccer",     "eng.1"),
    "LA LIGA": ("soccer",    "esp.1"),
    "LIGUE 1": ("soccer",    "fra.1"),
    "BUNDESLIGA": ("soccer", "ger.1"),
    "SERIE A": ("soccer",    "ita.1"),
    "CFL":    ("football",   "cfl"),
    "UFC":    None,
    "BOXING": None,
    "MMA":    None,
}


def _espn_sport_path(sport_league: str):
    """Return (sport, league) tuple for ESPN URL, or None if unsupported."""
    if not sport_league:
        return None
    sl = sport_league.strip().upper()
    for key, value in SPORT_LEAGUE_MAP.items():
        if sl.startswith(key) or key in sl:
            return value
    return None


# ---------------------------------------------------------------------------
# DB schema and helpers
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS posted_bets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sportsbook      TEXT NOT NULL,
    event           TEXT NOT NULL,
    bet_name        TEXT NOT NULL,
    market          TEXT NOT NULL,
    sport_league    TEXT,
    odds            TEXT,
    fair_odds       TEXT,
    ev_pct          TEXT,
    kelly_dollars   REAL,
    game_time       TEXT,
    game_time_epoch INTEGER,
    sportsbook_url  TEXT,
    source          TEXT,
    posted_at       INTEGER NOT NULL,
    result          TEXT NOT NULL DEFAULT 'pending',
    settled_at      INTEGER,
    settled_by      TEXT,
    profit_dollars  REAL,
    espn_event_id   TEXT,
    espn_checked_at INTEGER,
    needs_manual    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup
    ON posted_bets(sportsbook, event, bet_name, market, game_time);
CREATE INDEX IF NOT EXISTS idx_result     ON posted_bets(result);
CREATE INDEX IF NOT EXISTS idx_sportsbook ON posted_bets(sportsbook);
"""


def init_db(conn):
    """Create schema if it doesn't exist."""
    conn.executescript(_DDL)
    conn.commit()


def _parse_game_time_epoch(game_time_str: str) -> int | None:
    """
    Best-effort parse of CNO game_time strings like '7:30 PM ET' or 'Mon 7:30 PM'.
    Assumes Eastern time. If the parsed time is more than 2 hours in the past,
    assume it refers to tomorrow.
    Returns UTC unix timestamp or None on failure.
    """
    if not game_time_str:
        return None
    try:
        et = ZoneInfo("America/New_York")
        now_et = datetime.now(et)
        # Strip day-of-week prefix if present (e.g. "Mon 7:30 PM")
        s = re.sub(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+", "", game_time_str.strip(), flags=re.IGNORECASE)
        # Strip timezone suffix
        s = re.sub(r"\s+(ET|CT|MT|PT|EST|CST|MST|PST)$", "", s, flags=re.IGNORECASE).strip()
        parsed = datetime.strptime(s, "%I:%M %p")
        candidate = now_et.replace(
            hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0
        )
        if (now_et - candidate).total_seconds() > 7200:
            candidate += timedelta(days=1)
        return int(candidate.timestamp())
    except Exception:
        return None


def save_bet(conn, bet: dict, kelly_float: float | None):
    """Insert a bet into the tracker DB. Silently ignores duplicates."""
    conn.execute(
        """
        INSERT OR IGNORE INTO posted_bets
            (sportsbook, event, bet_name, market, sport_league, odds, fair_odds,
             ev_pct, kelly_dollars, game_time, game_time_epoch, sportsbook_url,
             source, posted_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            bet.get("sportsbook", "").strip(),
            bet.get("event", "").strip(),
            bet.get("bet_name", "").strip(),
            bet.get("market", "").strip(),
            bet.get("sport_league", "").strip(),
            bet.get("odds", "").strip(),
            bet.get("fair_odds", "").strip(),
            bet.get("ev_pct", "").strip(),
            kelly_float,
            bet.get("game_time", "").strip(),
            _parse_game_time_epoch(bet.get("game_time", "")),
            bet.get("sportsbook_url", "").strip(),
            bet.get("source", "").strip(),
            int(time.time()),
        ),
    )
    conn.commit()


def get_pending_bets(conn, limit: int = 10) -> list[dict]:
    """Return the oldest unsettled bets (up to limit)."""
    rows = conn.execute(
        "SELECT * FROM posted_bets WHERE result='pending' ORDER BY posted_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def settle_bet(conn, bet_id: int, result: str, settled_by: str, profit: float):
    """Settle a single bet. Only updates if still pending (race-condition safe)."""
    conn.execute(
        """
        UPDATE posted_bets
        SET result=?, settled_at=?, settled_by=?, profit_dollars=?
        WHERE id=? AND result='pending'
        """,
        (result, int(time.time()), settled_by, profit, bet_id),
    )
    conn.commit()


def calc_profit_for_result(result: str, kelly_dollars: float, odds_str: str) -> float:
    """Calculate profit in dollars given a result, stake, and American odds."""
    kd = kelly_dollars or 0.0
    if result in ("push", "void"):
        return 0.0
    if result == "loss":
        return -kd
    if result == "win":
        try:
            odds = int(str(odds_str).replace("+", "").strip())
            multiplier = (odds / 100) if odds > 0 else (100 / abs(odds))
            return round(kd * multiplier, 2)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def get_record_stats(conn) -> dict:
    """Return overall W/L/profit stats and per-sportsbook breakdown."""
    rows = conn.execute(
        "SELECT * FROM posted_bets WHERE result != 'pending'"
    ).fetchall()

    pending_count = conn.execute(
        "SELECT COUNT(*) FROM posted_bets WHERE result='pending'"
    ).fetchone()[0]
    manual_count = conn.execute(
        "SELECT COUNT(*) FROM posted_bets WHERE needs_manual=1 AND result='pending'"
    ).fetchone()[0]

    def _tally(bets):
        wins = losses = pushes = voids = 0
        profit = 0.0
        wagered = 0.0
        for b in bets:
            r = b["result"]
            p = b["profit_dollars"] or 0.0
            k = b["kelly_dollars"] or 0.0
            if r == "win":
                wins += 1
                profit += p
                wagered += k
            elif r == "loss":
                losses += 1
                profit += p
                wagered += k
            elif r == "push":
                pushes += 1
            elif r == "void":
                voids += 1
        roi = (profit / wagered * 100) if wagered > 0 else 0.0
        return {
            "wins": wins, "losses": losses, "pushes": pushes, "voids": voids,
            "profit": round(profit, 2), "wagered": round(wagered, 2),
            "roi": round(roi, 1),
        }

    all_bets = [dict(r) for r in rows]
    overall = _tally(all_bets)
    overall["pending"] = pending_count
    overall["needs_manual"] = manual_count

    by_book = {}
    for b in all_bets:
        book = b.get("sportsbook") or "Unknown"
        by_book.setdefault(book, []).append(b)

    return {
        "overall": overall,
        "by_sportsbook": {book: _tally(bets) for book, bets in by_book.items()},
    }


# ---------------------------------------------------------------------------
# ESPN API
# ---------------------------------------------------------------------------

def _espn_get(url: str) -> dict | None:
    """Fetch and parse JSON from an ESPN URL. Returns None on any failure."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=ESPN_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.debug("ESPN fetch failed (%s): %s", url, e)
        return None


def _fetch_scoreboard(sport: str, league: str) -> list[dict]:
    """Return list of ESPN competition dicts from the scoreboard endpoint."""
    data = _espn_get(f"{ESPN_BASE}/{sport}/{league}/scoreboard")
    if not data:
        return []
    events = []
    for ev in data.get("events", []):
        for comp in ev.get("competitions", []):
            comp["_espn_event_id"] = ev.get("id", "")
            comp["_espn_event_name"] = ev.get("name", "")
            events.append(comp)
    return events


def _fetch_summary(sport: str, league: str, event_id: str) -> dict | None:
    """Fetch full game summary (box score) for a specific ESPN event."""
    return _espn_get(f"{ESPN_BASE}/{sport}/{league}/summary?event={event_id}")


def _normalize(s: str) -> str:
    """Lowercase, strip diacritics, remove punctuation for fuzzy matching."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _match_event_to_espn(cno_event: str, espn_comps: list[dict]) -> str | None:
    """
    Fuzzy-match a CNO event string to an ESPN competition.
    Returns the ESPN event ID if exactly one unambiguous match is found.
    """
    parts = re.split(r"\s+[@vVsS]+\s+", cno_event, maxsplit=1)
    if len(parts) != 2:
        return None
    team_a = _normalize(parts[0])
    team_b = _normalize(parts[1])

    matches = []
    for comp in espn_comps:
        competitor_names = []
        for c in comp.get("competitors", []):
            team = c.get("team", {})
            for field in ("displayName", "shortDisplayName", "abbreviation", "name"):
                val = team.get(field, "")
                if val:
                    competitor_names.append(_normalize(val))

        combined = " ".join(competitor_names)
        # Both team fragments must appear in the combined competitor string
        if team_a and team_b and (
            any(team_a in n or n in team_a for n in competitor_names)
            and any(team_b in n or n in team_b for n in competitor_names)
        ):
            matches.append(comp["_espn_event_id"])

    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# Settlement parsers
# ---------------------------------------------------------------------------

def _parse_over_under(bet_name: str):
    """Extract ('over'|'under', float_line) from bet_name, or (None, None)."""
    m = re.search(r"\b(over|under)\s+([\d.]+)", bet_name, re.IGNORECASE)
    if not m:
        return None, None
    return m.group(1).lower(), float(m.group(2))


def _settle_total(bet_name: str, home_score: int, away_score: int) -> str | None:
    direction, line = _parse_over_under(bet_name)
    if direction is None or line is None:
        return None
    total = home_score + away_score
    if total == line:
        return "push"
    return "win" if (direction == "over" and total > line) or (direction == "under" and total < line) else "loss"


def _settle_moneyline(bet_name: str, home_team: str, away_team: str, winning_team: str) -> str | None:
    # Strip any trailing odds notation (+120, -115) from bet_name
    cleaned = re.sub(r"\s+[+-]\d+$", "", bet_name).strip()
    norm_pick = _normalize(cleaned)
    norm_winner = _normalize(winning_team)
    norm_home = _normalize(home_team)
    norm_away = _normalize(away_team)
    # Check if pick matches winner (substring both ways)
    if not norm_pick:
        return None
    pick_is_home = norm_pick in norm_home or norm_home in norm_pick
    pick_is_away = norm_pick in norm_away or norm_away in norm_pick
    winner_is_home = norm_winner in norm_home or norm_home in norm_winner
    if pick_is_home:
        return "win" if winner_is_home else "loss"
    if pick_is_away:
        return "win" if not winner_is_home else "loss"
    return None


def _settle_spread(bet_name: str, home_score: int, away_score: int,
                   home_team: str, away_team: str) -> str | None:
    # Expect bet_name like "Boston Celtics -5.5" or "Miami Heat +3"
    m = re.search(r"([+-]?\d+\.?\d*)\s*$", bet_name)
    if not m:
        return None
    spread = float(m.group(1))
    team_part = bet_name[:m.start()].strip()
    norm_pick = _normalize(team_part)
    norm_home = _normalize(home_team)
    norm_away = _normalize(away_team)
    pick_is_home = norm_pick in norm_home or norm_home in norm_pick
    if pick_is_home:
        diff = home_score - away_score
    else:
        diff = away_score - home_score
    adjusted = diff + spread
    if adjusted == 0:
        return "push"
    return "win" if adjusted > 0 else "loss"


# ESPN stat key mapping from CNO market name
_PLAYER_STAT_MAP = {
    "player points":              "points",
    "player rebounds":            "totalRebounds",
    "player assists":             "assists",
    "player 3-pointers made":     "threePointFieldGoalsMade",
    "player threes":              "threePointFieldGoalsMade",
    "player steals":              "steals",
    "player blocks":              "blocks",
    "player turnovers":           "turnovers",
    "player hits":                "hits",      # MLB
    "player strikeouts":          "strikeouts",
    "player home runs":           "homeRuns",
    "player goals":               "goals",     # NHL/soccer
    "player shots on goal":       "shotsOnGoal",
}


def _settle_player_prop(bet_name: str, market: str, player_stats: dict) -> str | None:
    """
    Settle a player prop.
    player_stats: {normalized_name: {stat_key: value, ...}}
    """
    market_key = _normalize(market)
    stat_key = _PLAYER_STAT_MAP.get(market_key)
    if stat_key is None:
        return None  # composite or unknown market

    direction, line = _parse_over_under(bet_name)
    if direction is None or line is None:
        return None

    # Extract player name: everything before "Over"/"Under" in bet_name
    name_match = re.match(r"^(.*?)\s+(?:over|under)", bet_name, re.IGNORECASE)
    if not name_match:
        return None
    player_name = _normalize(name_match.group(1))

    # Find player in stats (substring match on last name as fallback)
    stat_val = None
    for norm_name, stats in player_stats.items():
        if player_name in norm_name or norm_name in player_name:
            stat_val = stats.get(stat_key)
            break
        # Last-name fallback
        last = player_name.split()[-1] if player_name.split() else ""
        if last and len(last) > 3 and last in norm_name:
            stat_val = stats.get(stat_key)
            break

    if stat_val is None:
        return None

    try:
        actual = float(stat_val)
    except (TypeError, ValueError):
        return None

    if actual == line:
        return "push"
    return "win" if (direction == "over" and actual > line) or (direction == "under" and actual < line) else "loss"


def _extract_player_stats_from_summary(summary: dict) -> dict:
    """
    Parse ESPN summary JSON into {normalized_player_name: {stat_key: value}}.
    """
    players = {}
    for box in summary.get("boxscore", {}).get("players", []):
        for stat_group in box.get("statistics", []):
            keys = stat_group.get("keys", [])
            for athlete_entry in stat_group.get("athletes", []):
                athlete = athlete_entry.get("athlete", {})
                name = _normalize(athlete.get("displayName", ""))
                if not name:
                    continue
                stats = athlete_entry.get("stats", [])
                if name not in players:
                    players[name] = {}
                for i, key in enumerate(keys):
                    if i < len(stats):
                        try:
                            players[name][key] = float(stats[i])
                        except (ValueError, TypeError):
                            players[name][key] = stats[i]
    return players


def _get_game_result(summary: dict) -> dict | None:
    """
    Extract final score and winner from an ESPN summary.
    Returns dict with home_team, away_team, home_score, away_score, winner, completed.
    """
    try:
        comps = summary.get("header", {}).get("competitions", [])
        if not comps:
            return None
        comp = comps[0]
        status = comp.get("status", {})
        completed = status.get("type", {}).get("completed", False)

        home_team = away_team = ""
        home_score = away_score = 0
        winner = ""

        for c in comp.get("competitors", []):
            team_name = c.get("team", {}).get("displayName", "")
            score = int(c.get("score", 0) or 0)
            is_home = c.get("homeAway", "") == "home"
            if is_home:
                home_team = team_name
                home_score = score
            else:
                away_team = team_name
                away_score = score
            if c.get("winner"):
                winner = team_name

        return {
            "completed": completed,
            "home_team": home_team,
            "away_team": away_team,
            "home_score": home_score,
            "away_score": away_score,
            "winner": winner,
        }
    except Exception:
        return None


def settle_single_bet(conn, bet_row: dict) -> str:
    """
    Attempt ESPN auto-settlement for one pending bet.
    Returns: 'win' | 'loss' | 'push' | 'void' | 'needs_manual' | 'pending'
    Does NOT write to DB — caller is responsible for calling settle_bet().
    """
    sport_path = _espn_sport_path(bet_row.get("sport_league", ""))
    if sport_path is None:
        return "needs_manual"

    sport, league = sport_path

    # Skip if game hasn't started yet
    epoch = bet_row.get("game_time_epoch")
    if epoch and epoch > time.time():
        return "pending"

    # Use cached ESPN event ID or look it up
    event_id = bet_row.get("espn_event_id")
    if not event_id:
        events = _fetch_scoreboard(sport, league)
        event_id = _match_event_to_espn(bet_row.get("event", ""), events)
        if event_id:
            try:
                conn.execute(
                    "UPDATE posted_bets SET espn_event_id=?, espn_checked_at=? WHERE id=?",
                    (event_id, int(time.time()), bet_row["id"]),
                )
                conn.commit()
            except Exception:
                pass
        else:
            try:
                conn.execute(
                    "UPDATE posted_bets SET espn_checked_at=? WHERE id=?",
                    (int(time.time()), bet_row["id"]),
                )
                conn.commit()
            except Exception:
                pass
            return "pending"

    summary = _fetch_summary(sport, league, event_id)
    if not summary:
        return "pending"

    game = _get_game_result(summary)
    if not game or not game["completed"]:
        return "pending"

    market_lower = (bet_row.get("market") or "").lower()
    bet_name = bet_row.get("bet_name", "")

    if any(k in market_lower for k in ("total", "over/under", "over under")):
        result = _settle_total(bet_name, game["home_score"], game["away_score"])
    elif any(k in market_lower for k in ("moneyline", "money line")):
        result = _settle_moneyline(bet_name, game["home_team"], game["away_team"], game["winner"])
    elif any(k in market_lower for k in ("spread", "run line", "puck line", "handicap")):
        result = _settle_spread(bet_name, game["home_score"], game["away_score"],
                                game["home_team"], game["away_team"])
    elif market_lower.startswith("player"):
        player_stats = _extract_player_stats_from_summary(summary)
        result = _settle_player_prop(bet_name, bet_row.get("market", ""), player_stats)
    else:
        result = None

    if result is None:
        return "needs_manual"
    return result


# ---------------------------------------------------------------------------
# Batch settlement pass
# ---------------------------------------------------------------------------

def run_settlement_pass(conn) -> dict:
    """
    Attempt auto-settlement for all pending bets whose game_time has passed.
    Returns counts dict: {settled, pending, manual_flagged, errors}.
    """
    counts = {"settled": 0, "pending": 0, "manual_flagged": 0, "errors": 0}

    now = int(time.time())
    # Only attempt bets where game_time_epoch has passed, or game_time is unknown
    # and the bet was posted more than 4 hours ago (enough time for a game to finish)
    rows = conn.execute(
        """
        SELECT * FROM posted_bets
        WHERE result = 'pending'
          AND needs_manual = 0
          AND (
            (game_time_epoch IS NOT NULL AND game_time_epoch < ?)
            OR (game_time_epoch IS NULL AND posted_at < ?)
          )
        ORDER BY posted_at ASC
        """,
        (now, now - 4 * 3600),
    ).fetchall()

    for row in rows:
        bet = dict(row)
        try:
            outcome = settle_single_bet(conn, bet)
        except Exception as e:
            log.warning("Settlement error for bet #%s: %s", bet.get("id"), e)
            counts["errors"] += 1
            continue

        if outcome == "pending":
            counts["pending"] += 1
        elif outcome == "needs_manual":
            conn.execute(
                "UPDATE posted_bets SET needs_manual=1 WHERE id=?", (bet["id"],)
            )
            conn.commit()
            counts["manual_flagged"] += 1
        else:
            profit = calc_profit_for_result(outcome, bet.get("kelly_dollars") or 0.0,
                                            bet.get("odds") or "0")
            settle_bet(conn, bet["id"], outcome, "espn_auto", profit)
            counts["settled"] += 1
            log.info("Auto-settled bet #%s (%s %s @ %s) → %s  P/L: $%.2f",
                     bet["id"], bet.get("bet_name"), bet.get("market"),
                     bet.get("sportsbook"), outcome, profit)

    return counts
