"""SQLite persistence layer.

Stores sessions / hands / actions / coach_notes. Pure-stdlib (sqlite3).
Single connection guarded by a lock; the app is single-process anyway.

Schema is created on first connect via init_schema(). Migrations are not
implemented — for now we'd just rev the schema during early development.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    opponents      TEXT NOT NULL,
    small_blind    INTEGER NOT NULL DEFAULT 1,
    big_blind      INTEGER NOT NULL DEFAULT 2,
    starting_stack INTEGER NOT NULL DEFAULT 200
);

CREATE TABLE IF NOT EXISTS hands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    hand_number INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    button_seat INTEGER NOT NULL,
    board       TEXT NOT NULL,
    pot         INTEGER NOT NULL,
    winners     TEXT NOT NULL,
    seats       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hand_id      INTEGER NOT NULL REFERENCES hands(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    street       TEXT NOT NULL,
    seat         INTEGER NOT NULL,
    name         TEXT NOT NULL,
    action       TEXT NOT NULL,
    amount       INTEGER NOT NULL DEFAULT 0,
    to_amount    INTEGER NOT NULL DEFAULT 0,
    pot_after    INTEGER NOT NULL,
    ai_reasoning TEXT
);

CREATE TABLE IF NOT EXISTS coach_notes (
    hand_id    INTEGER PRIMARY KEY REFERENCES hands(id) ON DELETE CASCADE,
    note       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hands_session ON hands(session_id);
CREATE INDEX IF NOT EXISTS idx_actions_hand   ON actions(hand_id);
"""


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_db_path: Optional[Path] = None


def init(db_path: Path) -> None:
    """Open the DB and initialize schema. Call once at startup."""
    global _conn, _db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db_path = db_path
    _conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.executescript(SCHEMA)
    _conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- sessions ----------

def create_session(opponents: list[str], small_blind: int, big_blind: int,
                   starting_stack: int) -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO sessions (started_at, opponents, small_blind, big_blind, starting_stack) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), json.dumps(opponents), small_blind, big_blind, starting_stack),
        )
        _conn.commit()
        return cur.lastrowid


def list_sessions() -> list[dict]:
    """Return sessions newest-first, with hand counts and total chips_won for 'You'."""
    with _lock:
        rows = _conn.execute("""
            SELECT s.id, s.started_at, s.opponents, s.small_blind, s.big_blind,
                   s.starting_stack,
                   COUNT(h.id) AS hand_count
            FROM sessions s
            LEFT JOIN hands h ON h.session_id = s.id
            GROUP BY s.id
            ORDER BY s.id DESC
        """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["opponents"] = json.loads(d["opponents"])
        # add 'You' chips_won — needs a separate query because we'd otherwise
        # have to JSON-extract per row in SQLite which is awkward
        d["you_chips_won"] = _session_chips_won(d["id"], "You")
        out.append(d)
    return out


def _session_chips_won(session_id: int, player_name: str) -> int:
    """Sum chips_won for one player across all hands in a session."""
    with _lock:
        rows = _conn.execute(
            "SELECT seats FROM hands WHERE session_id = ?", (session_id,)
        ).fetchall()
    total = 0
    for r in rows:
        for seat in json.loads(r["seats"]):
            if seat["name"] == player_name:
                total += seat["chips_won"]
    return total


def get_session(session_id: int) -> Optional[dict]:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["opponents"] = json.loads(d["opponents"])
    return d


# ---------- hands + actions ----------

def insert_hand_and_actions(
    session_id: int,
    hand_number: int,
    started_at: str,
    button_seat: int,
    board: list[str],
    pot: int,
    winners: list[dict],
    seats: list[dict],
    actions: list[dict],
) -> int:
    """Atomically insert the hand and all its actions. Returns hand_id."""
    with _lock:
        cur = _conn.execute(
            "INSERT INTO hands (session_id, hand_number, started_at, button_seat, "
            "board, pot, winners, seats) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, hand_number, started_at, button_seat,
                " ".join(board), pot, json.dumps(winners), json.dumps(seats),
            ),
        )
        hand_id = cur.lastrowid
        for i, a in enumerate(actions):
            _conn.execute(
                "INSERT INTO actions (hand_id, seq, street, seat, name, action, "
                "amount, to_amount, pot_after, ai_reasoning) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    hand_id, i, a["street"], a["seat"], a["name"], a["action"],
                    a.get("amount", 0), a.get("to_amount", 0), a["pot_after"],
                    a.get("ai_reasoning"),
                ),
            )
        _conn.commit()
        return hand_id


def get_hands_for_session(session_id: int) -> list[dict]:
    """Hands in a session, in chronological order."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM hands WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["winners"] = json.loads(d["winners"])
        d["seats"] = json.loads(d["seats"])
        d["board"] = d["board"].split() if d["board"] else []
        out.append(d)
    return out


def get_actions_for_hand(hand_id: int) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM actions WHERE hand_id = ? ORDER BY seq ASC",
            (hand_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- coach notes ----------

def get_coach_note(hand_id: int) -> Optional[str]:
    with _lock:
        row = _conn.execute(
            "SELECT note FROM coach_notes WHERE hand_id = ?", (hand_id,)
        ).fetchone()
    return row["note"] if row else None


def save_coach_note(hand_id: int, note: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO coach_notes (hand_id, note, created_at) "
            "VALUES (?, ?, ?)",
            (hand_id, note, _now()),
        )
        _conn.commit()


# ---------- bulk reads (for stats reconstruction at startup) ----------

def all_hands_with_actions(session_id: Optional[int] = None) -> list[tuple[dict, list[dict]]]:
    """For each hand (optionally filtered by session), return (hand_dict, actions_list).
    Used to rebuild StatsTracker state on startup or session switch."""
    if session_id is None:
        hands = []
        with _lock:
            rows = _conn.execute("SELECT * FROM hands ORDER BY id ASC").fetchall()
        for r in rows:
            d = dict(r)
            d["winners"] = json.loads(d["winners"])
            d["seats"] = json.loads(d["seats"])
            d["board"] = d["board"].split() if d["board"] else []
            hands.append(d)
    else:
        hands = get_hands_for_session(session_id)

    out = []
    for h in hands:
        actions = get_actions_for_hand(h["id"])
        out.append((h, actions))
    return out
