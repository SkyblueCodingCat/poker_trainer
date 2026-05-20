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
from game import (
    GameState, Player, Street, apply_action, legal_actions, start_new_hand, view_for,
)
from personalities import PERSONALITIES, PERSONALITY_LABELS
from stats import StatsTracker, compute_hand_stats


HERE = Path(__file__).resolve().parent
FRONTEND = HERE.parent / "frontend"

app = FastAPI(title="Poker Trainer")

# ---- single-session state ----

_lock = threading.Lock()
_state: Optional[GameState] = None
_button: int = 0
_rng = random.Random()
_starting_stacks: list[int] = []  # stacks at the start of the current hand (before blinds)
_stats = StatsTracker()
_opponent_personalities: list[str] = ["nit", "gto"]  # current 2 opponents (seats 1, 2)
HUMAN_SEAT = 0


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
    global _state, _button, _starting_stacks
    if _state is None:
        players = _make_players()
        _starting_stacks = [p.stack for p in players]
        _state = start_new_hand(players, button=_button, hand_number=1, rng=_rng)


def _maybe_record_hand() -> None:
    """If the current hand just ended, record stats once."""
    if _state is None:
        return
    if _state.street != Street.HAND_OVER:
        return
    if getattr(_state, "_stats_recorded", False):
        return
    # but blinds were posted, so player.stack at start was higher than now;
    # we stored _starting_stacks before posting blinds.
    # Actually we stored AFTER make_players() = before start_new_hand posts blinds.
    # Wait — _starting_stacks captures the stacks at the moment of new-hand creation,
    # which is what compute_hand_stats wants (chips before any forced bets).
    hand_stats = compute_hand_stats(_state, _starting_stacks)
    _stats.record_hand(hand_stats)
    _state.__dict__["_stats_recorded"] = True


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
    with _lock:
        _button = 0
        _stats.reset()
        if req and req.opponents:
            if len(req.opponents) != 2:
                raise HTTPException(400, "must supply exactly 2 opponents")
            for o in req.opponents:
                if o not in PERSONALITIES:
                    raise HTTPException(400, f"unknown personality: {o}")
            _opponent_personalities = list(req.opponents)
        players = _make_players()
        _starting_stacks = [p.stack for p in players]
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
    global _state, _button, _starting_stacks
    with _lock:
        if _state is None:
            _ensure_state()
            return get_state_unlocked()
        # finalize stats from the prior hand if not yet
        _maybe_record_hand()
        # carry stacks forward
        stacks = [p.stack for p in _state.players]
        if all(s == 0 for s in stacks):
            raise HTTPException(400, "all players busted")
        # rotate button to next seat with chips
        n = len(stacks)
        _button = (_state.button + 1) % n
        while stacks[_button] == 0:
            _button = (_button + 1) % n
        next_hand = (_state.hand_number or 0) + 1
        players = _make_players(stacks)
        _starting_stacks = [p.stack for p in players]
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
def get_stats():
    with _lock:
        bb = _state.big_blind if _state else 2
        return _stats.snapshot(big_blind=bb)


@app.get("/api/coach")
async def coach():
    with _lock:
        if _state is None or _state.street != Street.HAND_OVER:
            raise HTTPException(400, "no completed hand to review")
        if not _state.revealed_cards:
            # uncontested win — minimal review
            return {"note": "这手没有走到摊牌——对手在翻牌前或下注早期就弃牌了，你没有真正的关键决策点可以深入复盘。\n\n小提示：如果你这手是开局加注偷盲成功，那是好事；如果你是被偷盲后弃掉了 BB，可以观察一下哪些位置/对手在频繁偷你，下手考虑做 3-bet defend。"}
        snapshot = _state
    note = await asyncio.to_thread(ai_player.coach_review, snapshot, HUMAN_SEAT)
    with _lock:
        _state.coach_note = note
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
