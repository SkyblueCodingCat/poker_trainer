"""FastAPI server for the poker trainer.

Holds a single in-memory Game session. Endpoints:
  POST /api/new-game     reset stacks, start hand 1
  POST /api/new-hand     deal next hand (rotates button, keeps stacks)
  GET  /api/state        full state from human's POV
  POST /api/action       human plays an action {action, amount}
  POST /api/ai-act       run the next AI's decision and apply it
  GET  /api/coach        on-demand coach review for last hand
  GET  /                 serves the frontend
"""
from __future__ import annotations

import asyncio
import os
import random
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Load .env from the project root before any module that reads env vars
# (ai_player / llm) gets imported. Existing shell vars take precedence.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv optional; fall back to shell env

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai_player
import db
from game import (
    GameState, Player, Street, apply_action, legal_actions, start_new_hand, view_for,
)
from personalities import PERSONALITIES, PERSONALITY_LABELS
from stats import StatsTracker, compute_hand_stats, compute_hand_stats_from_record


HERE = Path(__file__).resolve().parent
FRONTEND = HERE.parent / "frontend"
DATA_DIR = HERE.parent / "data"
DB_PATH = DATA_DIR / "poker.db"

app = FastAPI(title="Poker Trainer")

# ---- single-session state ----

_lock = threading.Lock()
_state: Optional[GameState] = None
_button: int = 0
_rng = random.Random()
_starting_stacks: list[int] = []  # stacks at the start of the current hand (before blinds)
_starting_stack_amount: int = 200
_stats = StatsTracker()
_opponent_personalities: list[str] = ["nit", "gto"]  # current 2 opponents (seats 1, 2)
_session_id: Optional[int] = None  # current DB session id
_hand_started_at: Optional[str] = None  # ISO timestamp captured when hand begins
HUMAN_SEAT = 0


# ---- DB init ----

@app.on_event("startup")
def on_startup():
    db.init(DB_PATH)


def _stats_for_session(session_id: int) -> StatsTracker:
    """Build a fresh StatsTracker by replaying all hands in `session_id`."""
    tracker = StatsTracker()
    for hand, actions in db.all_hands_with_actions(session_id=session_id):
        try:
            hand_stats = compute_hand_stats_from_record(hand, actions)
            tracker.record_hand(hand_stats)
        except Exception as e:
            print(f"[stats replay] skipping hand {hand.get('id')}: {e!r}")
    return tracker


def _make_players(stacks: Optional[list[int]] = None,
                  opponents: Optional[list[str]] = None) -> list[Player]:
    s = stacks or [200, 200, 200]
    opps = opponents or _opponent_personalities
    if len(opps) != 2:
        raise ValueError("need exactly 2 opponent personalities")
    for o in opps:
        if o not in PERSONALITIES:
            raise ValueError(f"unknown personality {o!r}")
    return [
        Player(name="You", is_human=True, personality=None, stack=s[0]),
        Player(
            name=PERSONALITY_LABELS[opps[0]]["name"],
            is_human=False, personality=opps[0], stack=s[1],
        ),
        Player(
            name=PERSONALITY_LABELS[opps[1]]["name"],
            is_human=False, personality=opps[1], stack=s[2],
        ),
    ]


def _ensure_state():
    global _state, _button, _starting_stacks, _hand_started_at, _session_id
    if _state is None:
        # First hand of a fresh process — make sure a session exists in DB.
        if _session_id is None:
            _session_id = db.create_session(
                opponents=list(_opponent_personalities),
                small_blind=1,
                big_blind=2,
                starting_stack=_starting_stack_amount,
            )
        players = _make_players()
        _starting_stacks = [p.stack for p in players]
        _hand_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _state = start_new_hand(players, button=_button, hand_number=1, rng=_rng)


def _maybe_record_hand() -> None:
    """If the current hand just ended, record stats + persist to DB once."""
    if _state is None:
        return
    if _state.street != Street.HAND_OVER:
        return
    if getattr(_state, "_stats_recorded", False):
        return

    # 1. compute and accumulate stats in memory (drives the live data panel)
    hand_stats = compute_hand_stats(_state, _starting_stacks)
    _stats.record_hand(hand_stats)
    _state.__dict__["_stats_recorded"] = True

    # 2. persist to DB
    if _session_id is None:
        return  # shouldn't happen, but be safe
    seats_payload = [
        {
            "seat": i,
            "name": p.name,
            "personality": p.personality,
            "hole_cards": [str(c) for c in p.hole_cards],
            "starting_stack": _starting_stacks[i],
            "ending_stack": p.stack,
            "chips_won": p.stack - _starting_stacks[i],
        }
        for i, p in enumerate(_state.players)
    ]
    actions_payload = []
    ai_log = getattr(_state, "_ai_log", {}) or {}
    for idx, h in enumerate(_state.history):
        actions_payload.append({
            "street": h.street.value,
            "seat": h.seat,
            "name": h.name,
            "action": h.action,
            "amount": h.amount,
            "to_amount": h.to_amount,
            "pot_after": h.pot_after,
            "ai_reasoning": ai_log.get(idx),
        })
    hand_id = db.insert_hand_and_actions(
        session_id=_session_id,
        hand_number=_state.hand_number,
        started_at=_hand_started_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        button_seat=_state.button,
        board=[str(c) for c in _state.board],
        pot=sum(s["chips_won"] for s in seats_payload if s["chips_won"] > 0),
        winners=_state.winners,
        seats=seats_payload,
        actions=actions_payload,
    )
    _state.__dict__["_db_hand_id"] = hand_id


# ---- request models ----

class ActionRequest(BaseModel):
    action: str
    amount: int = 0


class NewGameRequest(BaseModel):
    opponents: Optional[list[str]] = None  # e.g. ["nit", "lag"]


# ---- routes ----

@app.post("/api/new-game")
def new_game(req: Optional[NewGameRequest] = None):
    global _state, _button, _starting_stacks, _opponent_personalities
    global _session_id, _hand_started_at, _stats
    with _lock:
        # finalize any in-flight hand from the previous session before resetting
        _maybe_record_hand()
        _button = 0
        if req and req.opponents:
            if len(req.opponents) != 2:
                raise HTTPException(400, "must supply exactly 2 opponents")
            for o in req.opponents:
                if o not in PERSONALITIES:
                    raise HTTPException(400, f"unknown personality: {o}")
            _opponent_personalities = list(req.opponents)
        # new DB session
        _session_id = db.create_session(
            opponents=list(_opponent_personalities),
            small_blind=1, big_blind=2, starting_stack=_starting_stack_amount,
        )
        # reset in-memory stats so the current panel shows just this session
        _stats = StatsTracker()
        players = _make_players()
        _starting_stacks = [p.stack for p in players]
        _hand_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _state = start_new_hand(players, button=_button, hand_number=1, rng=_rng)
        return get_state_unlocked()


@app.get("/api/personalities")
def list_personalities():
    """Catalog of available opponent personalities for the picker UI."""
    return {
        "personalities": [
            {"key": k, **PERSONALITY_LABELS[k]}
            for k in PERSONALITIES
        ],
        "current": _opponent_personalities,
    }


@app.post("/api/new-hand")
def new_hand():
    global _state, _button, _starting_stacks, _hand_started_at
    with _lock:
        if _state is None:
            _ensure_state()
            return get_state_unlocked()
        # finalize + persist the prior hand if not yet
        _maybe_record_hand()
        stacks = [p.stack for p in _state.players]
        if all(s == 0 for s in stacks):
            raise HTTPException(400, "all players busted")
        n = len(stacks)
        _button = (_state.button + 1) % n
        while stacks[_button] == 0:
            _button = (_button + 1) % n
        next_hand = (_state.hand_number or 0) + 1
        players = _make_players(stacks)
        _starting_stacks = [p.stack for p in players]
        _hand_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _state = start_new_hand(players, button=_button, hand_number=next_hand, rng=_rng)
        return get_state_unlocked()


@app.get("/api/state")
def get_state():
    with _lock:
        _ensure_state()
        v = view_for(_state, HUMAN_SEAT)
        # also include legal actions for the human if it's their turn
        if _state.to_act == HUMAN_SEAT and _state.street not in (Street.SHOWDOWN, Street.HAND_OVER):
            v["your_legal_actions"] = legal_actions(_state, HUMAN_SEAT)
        else:
            v["your_legal_actions"] = None
        # who's the next AI to act? frontend uses this to know to call /api/ai-act
        v["ai_to_act"] = (
            _state.to_act
            if _state.to_act != HUMAN_SEAT and _state.street not in (Street.SHOWDOWN, Street.HAND_OVER)
            else None
        )
        return v


@app.post("/api/action")
def human_action(req: ActionRequest):
    with _lock:
        _ensure_state()
        if _state.to_act != HUMAN_SEAT:
            raise HTTPException(400, "not your turn")
        try:
            apply_action(_state, HUMAN_SEAT, req.action, req.amount)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _maybe_record_hand()
        return get_state_unlocked()


@app.post("/api/ai-act")
async def ai_act():
    """Drive the next AI action. Run model call off the event loop."""
    # snapshot the seat under lock, then call the model without holding the lock,
    # then re-acquire to apply.
    with _lock:
        _ensure_state()
        if _state.street in (Street.SHOWDOWN, Street.HAND_OVER):
            return get_state_unlocked()
        seat = _state.to_act
        if seat == HUMAN_SEAT:
            raise HTTPException(400, "human's turn")
        snapshot = _state  # GameState is mutable; we'll just use it directly since model is read-only
    try:
        decision = await asyncio.to_thread(ai_player.decide, snapshot, seat)
    except Exception as e:
        raise HTTPException(500, f"AI error: {e!r}")
    with _lock:
        # validate seat hasn't shifted (shouldn't happen in single-thread access pattern)
        if _state.to_act != seat:
            return get_state_unlocked()
        try:
            apply_action(_state, seat, decision["action"], decision["amount"])
        except ValueError as e:
            # safety fallback: fold/check
            legal = legal_actions(_state, seat)
            types = {a["type"] for a in legal["actions"]}
            if "check" in types:
                apply_action(_state, seat, "check")
            else:
                apply_action(_state, seat, "fold")
            decision["action"] = "check" if "check" in types else "fold"
            decision["reasoning"] = f"(parse error: {e}; defaulted)"
        # attach reasoning to last history entry as a side-channel
        _state.history[-1].name  # touch
        # store on state for frontend
        ai_log = getattr(_state, "_ai_log", None)
        if ai_log is None:
            ai_log = {}
            _state.__dict__["_ai_log"] = ai_log
        ai_log[len(_state.history) - 1] = decision["reasoning"]
        _maybe_record_hand()
        v = get_state_unlocked()
        v["last_ai"] = {
            "seat": seat,
            "name": _state.players[seat].name,
            "action": decision["action"],
            "amount": decision["amount"],
            "reasoning": decision["reasoning"],
        }
        return v


@app.get("/api/stats")
def get_stats(session_id: Optional[int] = None):
    """Return stats. Default = current live session. With session_id, replay
    that session from DB (read-only)."""
    with _lock:
        bb = _state.big_blind if _state else 2
        if session_id is None or session_id == _session_id:
            payload = _stats.snapshot(big_blind=bb)
            payload["session_id"] = _session_id
            return payload
    # replay outside the lock — the DB has its own lock
    tracker = _stats_for_session(session_id)
    sess = db.get_session(session_id)
    bb = sess["big_blind"] if sess else 2
    payload = tracker.snapshot(big_blind=bb)
    payload["session_id"] = session_id
    return payload


@app.get("/api/sessions")
def list_sessions():
    sessions = db.list_sessions()
    # enrich each with the last hand's ending stacks so the UI can show
    # "resume with You=$X, Stone=$Y" and disable resume if all busted
    for s in sessions:
        last = db.last_hand_ending_stacks(s["id"])
        if last:
            s["last_stacks"] = {
                seat["name"]: seat["ending_stack"] for seat in last["seats"]
            }
            s["last_button_seat"] = last["button_seat"]
            s["last_hand_number"] = last["hand_number"]
        else:
            s["last_stacks"] = None
            s["last_button_seat"] = None
            s["last_hand_number"] = 0
    return {"sessions": sessions, "current": _session_id}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: int):
    """Delete a session and all its data. Refuse if it's the live one."""
    with _lock:
        if session_id == _session_id and _state is not None:
            raise HTTPException(400, "不能删除当前正在进行的会话；先开始一个新局再删除")
    ok = db.delete_session(session_id)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"deleted": session_id}


@app.post("/api/sessions/{session_id}/resume")
def resume_session(session_id: int):
    """Continue a previous session: open a new hand using its last ending
    stacks and rotate the button. The opponent personalities are taken from
    the saved session record."""
    global _state, _button, _starting_stacks, _opponent_personalities
    global _session_id, _hand_started_at, _stats
    with _lock:
        if _state is not None and _state.street != Street.HAND_OVER:
            raise HTTPException(400, "当前正在打牌中，先打完这一手再切换会话")

        sess = db.get_session(session_id)
        if not sess:
            raise HTTPException(404, "session not found")
        last = db.last_hand_ending_stacks(session_id)

        # opponents come from the saved session
        _opponent_personalities = list(sess["opponents"])

        # determine starting stacks for the next hand
        if last is None:
            # session has no hands yet — start fresh with default stacks
            stacks = [sess["starting_stack"]] * 3
            next_button = 0
            next_hand_number = 1
        else:
            # map name → ending_stack
            by_name = {s["name"]: s["ending_stack"] for s in last["seats"]}
            # build stacks in seat order matching _make_players (seat 0 = You,
            # seat 1 = first opponent, seat 2 = second opponent)
            opp1_name = PERSONALITY_LABELS[_opponent_personalities[0]]["name"]
            opp2_name = PERSONALITY_LABELS[_opponent_personalities[1]]["name"]
            stacks = [
                by_name.get("You", sess["starting_stack"]),
                by_name.get(opp1_name, sess["starting_stack"]),
                by_name.get(opp2_name, sess["starting_stack"]),
            ]
            if all(s == 0 for s in stacks):
                raise HTTPException(400, "all players busted in that session")
            # rotate button forward from last hand's button (skip busted seats)
            next_button = (last["button_seat"] + 1) % 3
            while stacks[next_button] == 0:
                next_button = (next_button + 1) % 3
            next_hand_number = last["hand_number"] + 1

        _session_id = session_id
        _button = next_button
        # rebuild in-memory stats tracker for this session from DB so the data
        # panel shows full historical stats, not just the new hand
        _stats = _stats_for_session(session_id)

        players = _make_players(stacks=stacks, opponents=_opponent_personalities)
        _starting_stacks = [p.stack for p in players]
        _hand_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _state = start_new_hand(
            players, button=_button, hand_number=next_hand_number, rng=_rng,
        )
        return get_state_unlocked()


@app.get("/api/coach")
async def coach():
    with _lock:
        if _state is None or _state.street != Street.HAND_OVER:
            raise HTTPException(400, "no completed hand to review")
        # if we already cached a note for this hand, return it
        hand_id = getattr(_state, "_db_hand_id", None)
        if hand_id is not None:
            cached = db.get_coach_note(hand_id)
            if cached:
                return {"note": cached, "cached": True}
        if not _state.revealed_cards:
            # uncontested win — minimal review (still cache to avoid re-asking)
            note = "这手没有走到摊牌——对手在翻牌前或下注早期就弃牌了，你没有真正的关键决策点可以深入复盘。\n\n小提示：如果你这手是开局加注偷盲成功，那是好事；如果你是被偷盲后弃掉了 BB，可以观察一下哪些位置/对手在频繁偷你，下手考虑做 3-bet defend。"
            if hand_id is not None:
                db.save_coach_note(hand_id, note)
            return {"note": note}
        snapshot = _state
    note = await asyncio.to_thread(ai_player.coach_review, snapshot, HUMAN_SEAT)
    with _lock:
        _state.coach_note = note
        hand_id = getattr(_state, "_db_hand_id", None)
        if hand_id is not None:
            db.save_coach_note(hand_id, note)
    return {"note": note}


def get_state_unlocked() -> dict:
    """Same as /api/state but assumes the lock is already held."""
    v = view_for(_state, HUMAN_SEAT)
    if _state.to_act == HUMAN_SEAT and _state.street not in (Street.SHOWDOWN, Street.HAND_OVER):
        v["your_legal_actions"] = legal_actions(_state, HUMAN_SEAT)
    else:
        v["your_legal_actions"] = None
    v["ai_to_act"] = (
        _state.to_act
        if _state.to_act != HUMAN_SEAT and _state.street not in (Street.SHOWDOWN, Street.HAND_OVER)
        else None
    )
    # include AI reasoning log so frontend can show it next to action history
    v["ai_reasoning"] = getattr(_state, "_ai_log", {}) or {}
    return v


# ---- static frontend ----

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="static")
