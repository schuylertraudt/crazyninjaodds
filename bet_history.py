#!/usr/bin/env python3
"""
Persistent bet history for the EV Ninja auto-post loop.
Stores every auto-posted bet in a local SQLite database so performance
reports can be generated across restarts.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("bet_history.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                posted_at   REAL    NOT NULL,
                event       TEXT,
                market      TEXT,
                bet_name    TEXT,
                sportsbook  TEXT,
                odds        TEXT,
                fair_odds   TEXT,
                ev_pct      TEXT,
                kelly       TEXT,
                sport_league TEXT,
                game_time   TEXT
            )
        """)
        conn.commit()


def log_bet(bet: dict) -> None:
    """Record a single auto-posted bet."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO bets
                (posted_at, event, market, bet_name, sportsbook,
                 odds, fair_odds, ev_pct, kelly, sport_league, game_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).timestamp(),
                bet.get("event", ""),
                bet.get("market", ""),
                bet.get("bet_name", ""),
                bet.get("sportsbook", ""),
                bet.get("odds", ""),
                bet.get("fair_odds", ""),
                bet.get("ev_pct", ""),
                bet.get("kelly", ""),
                bet.get("sport_league", ""),
                bet.get("game_time", ""),
            ),
        )
        conn.commit()


def get_bets_since(timestamp: float) -> list[dict]:
    """Return all bets posted at or after the given Unix timestamp."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bets WHERE posted_at >= ? ORDER BY posted_at",
            (timestamp,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_bets() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM bets ORDER BY posted_at").fetchall()
    return [dict(r) for r in rows]
