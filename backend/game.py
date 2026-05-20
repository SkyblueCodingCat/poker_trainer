"""No-Limit Texas Hold'em engine.

Pure logic, no I/O. The web layer drives this by calling apply_action() and
reading state. Designed for 3-handed cash; supports any 2-9 players in theory.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import List, Optional, Tuple


# ---------- cards ----------

RANKS = "23456789TJQKA"
SUITS = "shdc"  # spades, hearts, diamonds, clubs
RANK_VALUE = {r: i for i, r in enumerate(RANKS, start=2)}


@dataclass(frozen=True)
class Card:
    rank: str  # one of RANKS
    suit: str  # one of SUITS

    def __str__(self) -> str:
        return self.rank + self.suit

    @property
    def value(self) -> int:
        return RANK_VALUE[self.rank]


def make_deck() -> List[Card]:
    return [Card(r, s) for r in RANKS for s in SUITS]


# ---------- hand evaluation ----------

class HandRank(Enum):
    HIGH_CARD = 1
    PAIR = 2
    TWO_PAIR = 3
    TRIPS = 4
    STRAIGHT = 5
    FLUSH = 6
    FULL_HOUSE = 7
    QUADS = 8
    STRAIGHT_FLUSH = 9


def _rank_5(cards: List[Card]) -> Tuple[int, ...]:
    """Return a comparable tuple for any 5 cards. Higher tuple = better."""
    assert len(cards) == 5
    values = sorted((c.value for c in cards), reverse=True)
    suits = [c.suit for c in cards]
    flush = len(set(suits)) == 1

    # straight detection — handle wheel A-2-3-4-5
    distinct = sorted(set(values), reverse=True)
    straight_high = 0
    if len(distinct) == 5:
        if distinct[0] - distinct[4] == 4:
            straight_high = distinct[0]
        elif distinct == [14, 5, 4, 3, 2]:
            straight_high = 5  # wheel

    # group by count
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    # sort by (count desc, value desc)
    by_count = sorted(counts.items(), key=lambda kv: (-kv[1], -kv[0]))
    count_pattern = tuple(c for _, c in by_count)
    ordered_vals = tuple(v for v, _ in by_count)

    if flush and straight_high:
        return (HandRank.STRAIGHT_FLUSH.value, straight_high)
    if count_pattern == (4, 1):
        return (HandRank.QUADS.value, ordered_vals[0], ordered_vals[1])
    if count_pattern == (3, 2):
        return (HandRank.FULL_HOUSE.value, ordered_vals[0], ordered_vals[1])
    if flush:
        return (HandRank.FLUSH.value, *values)
    if straight_high:
        return (HandRank.STRAIGHT.value, straight_high)
    if count_pattern == (3, 1, 1):
        return (HandRank.TRIPS.value, ordered_vals[0], *ordered_vals[1:])
    if count_pattern == (2, 2, 1):
        return (HandRank.TWO_PAIR.value, ordered_vals[0], ordered_vals[1], ordered_vals[2])
    if count_pattern == (2, 1, 1, 1):
        return (HandRank.PAIR.value, ordered_vals[0], *ordered_vals[1:])
    return (HandRank.HIGH_CARD.value, *values)


def best_hand(cards: List[Card]) -> Tuple[Tuple[int, ...], List[Card]]:
    """Best 5-card combination from 5–7 cards. Returns (rank tuple, the 5 cards)."""
    best_tuple: Tuple[int, ...] = ()
    best_combo: List[Card] = []
    for combo in combinations(cards, 5):
        t = _rank_5(list(combo))
        if t > best_tuple:
            best_tuple = t
            best_combo = list(combo)
    return best_tuple, best_combo


HAND_NAME = {
    HandRank.HIGH_CARD.value: "High Card",
    HandRank.PAIR.value: "Pair",
    HandRank.TWO_PAIR.value: "Two Pair",
    HandRank.TRIPS.value: "Three of a Kind",
    HandRank.STRAIGHT.value: "Straight",
    HandRank.FLUSH.value: "Flush",
    HandRank.FULL_HOUSE.value: "Full House",
    HandRank.QUADS.value: "Four of a Kind",
    HandRank.STRAIGHT_FLUSH.value: "Straight Flush",
}


# ---------- game state ----------

class Street(Enum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"
    HAND_OVER = "hand_over"


@dataclass
class Player:
    name: str
    is_human: bool
    personality: Optional[str] = None  # "nit" / "gto" / etc; None for human
    stack: int = 200
    hole_cards: List[Card] = field(default_factory=list)
    # per-hand state:
    folded: bool = False
    all_in: bool = False
    invested_this_street: int = 0  # chips put in current betting round
    invested_total: int = 0  # chips put in this hand total (for sidepot calc)


@dataclass
class ActionRecord:
    seat: int
    name: str
    street: Street
    action: str  # fold/check/call/bet/raise/post
    amount: int = 0  # additional chips put in by this action (0 for check/fold)
    to_amount: int = 0  # the new "current_bet" after this action
    pot_after: int = 0


@dataclass
class GameState:
    players: List[Player]
    button: int = 0  # seat index of dealer button
    small_blind: int = 1
    big_blind: int = 2
    deck: List[Card] = field(default_factory=list)
    board: List[Card] = field(default_factory=list)
    pot: int = 0
    street: Street = Street.PREFLOP
    current_bet: int = 0  # highest invested_this_street amount this round
    last_raise_size: int = 0  # for min-raise validation
    to_act: int = 0  # seat index whose turn it is
    last_aggressor: Optional[int] = None
    history: List[ActionRecord] = field(default_factory=list)
    hand_number: int = 0
    # who has acted at the current betting level — when everyone in has acted
    # and matched current_bet, the street ends
    acted_this_street: set = field(default_factory=set)
    # showdown / result
    winners: List[dict] = field(default_factory=list)  # [{seats:[..], amount, reason}]
    revealed_cards: dict = field(default_factory=dict)  # seat -> hole cards str list
    # coaching note added at hand end
    coach_note: str = ""

    def player(self, seat: int) -> Player:
        return self.players[seat]

    def active_seats(self) -> List[int]:
        return [i for i, p in enumerate(self.players) if not p.folded]

    def can_act_seats(self) -> List[int]:
        """Seats that can still take voluntary actions (not folded, not all-in)."""
        return [i for i, p in enumerate(self.players) if not p.folded and not p.all_in]


# ---------- engine ----------

def start_new_hand(players: List[Player], button: int, sb: int = 1, bb: int = 2,
                   hand_number: int = 1, rng: Optional[random.Random] = None) -> GameState:
    """Reset per-hand state and post blinds. Players' stacks persist."""
    rng = rng or random.Random()
    for p in players:
        p.hole_cards = []
        p.folded = p.stack == 0  # busted players sit out
        p.all_in = False
        p.invested_this_street = 0
        p.invested_total = 0

    deck = make_deck()
    rng.shuffle(deck)

    state = GameState(
        players=players,
        button=button,
        small_blind=sb,
        big_blind=bb,
        deck=deck,
        hand_number=hand_number,
    )

    # deal 2 cards each, starting left of button
    n = len(players)
    for _ in range(2):
        for offset in range(1, n + 1):
            seat = (button + offset) % n
            if not players[seat].folded:
                players[seat].hole_cards.append(state.deck.pop())

    # post blinds
    sb_seat, bb_seat, first_to_act = _blind_seats(state)
    _post(state, sb_seat, sb, "post-sb")
    _post(state, bb_seat, bb, "post-bb")
    state.current_bet = bb
    state.last_raise_size = bb
    state.to_act = first_to_act
    state.last_aggressor = bb_seat  # bb closes preflop action if uncontested
    state.acted_this_street = set()
    return state


def _blind_seats(state: GameState) -> Tuple[int, int, int]:
    """Return (sb_seat, bb_seat, first_to_act_preflop). Heads-up special-cased."""
    n = len(state.players)
    active = [i for i, p in enumerate(state.players) if p.stack > 0]
    if len(active) == 2:
        # heads-up: button posts SB, other posts BB, button acts first preflop
        sb_seat = state.button
        bb_seat = (state.button + 1) % n
        first = sb_seat
    else:
        sb_seat = (state.button + 1) % n
        bb_seat = (state.button + 2) % n
        first = (state.button + 3) % n
    # skip seats with 0 stack
    while state.players[first].stack == 0 and first not in (sb_seat, bb_seat):
        first = (first + 1) % n
    return sb_seat, bb_seat, first


def _post(state: GameState, seat: int, amount: int, label: str) -> None:
    p = state.player(seat)
    chips = min(amount, p.stack)
    p.stack -= chips
    p.invested_this_street += chips
    p.invested_total += chips
    state.pot += chips
    if p.stack == 0:
        p.all_in = True
    state.history.append(ActionRecord(
        seat=seat, name=p.name, street=state.street,
        action=label, amount=chips, to_amount=p.invested_this_street,
        pot_after=state.pot,
    ))


# ---------- action validation + application ----------

def legal_actions(state: GameState, seat: int) -> dict:
    """Describe legal actions for `seat` right now."""
    if state.street in (Street.SHOWDOWN, Street.HAND_OVER):
        return {"actions": []}
    p = state.player(seat)
    if p.folded or p.all_in or seat != state.to_act:
        return {"actions": []}

    to_call = state.current_bet - p.invested_this_street
    actions = []

    if to_call == 0:
        actions.append({"type": "check"})
    else:
        actions.append({"type": "fold"})
        actions.append({"type": "call", "amount": min(to_call, p.stack)})

    # raise/bet sizing
    min_raise_to = state.current_bet + max(state.last_raise_size, state.big_blind)
    max_raise_to = p.invested_this_street + p.stack  # all-in cap

    if state.current_bet == 0:
        # opening bet
        min_bet = state.big_blind
        max_bet = p.stack
        if max_bet >= min_bet:
            actions.append({
                "type": "bet",
                "min": min_bet,
                "max": max_bet,
            })
        if 0 not in [a.get("amount") for a in actions if a["type"] == "call"]:
            # already added check above
            pass
    else:
        if max_raise_to > state.current_bet:
            actions.append({
                "type": "raise",
                "min_to": min(min_raise_to, max_raise_to),
                "max_to": max_raise_to,
            })
            if to_call > 0 and p.stack > to_call:
                pass  # fold/call already added

    return {
        "actions": actions,
        "to_call": to_call,
        "pot": state.pot,
        "current_bet": state.current_bet,
        "your_stack": p.stack,
        "your_invested_this_street": p.invested_this_street,
    }


def apply_action(state: GameState, seat: int, action: str, amount: int = 0) -> None:
    """Mutate state with the given action. Raises ValueError on invalid input."""
    if seat != state.to_act:
        raise ValueError(f"not seat {seat}'s turn (to_act={state.to_act})")
    p = state.player(seat)
    if p.folded or p.all_in:
        raise ValueError("player can't act")

    to_call = state.current_bet - p.invested_this_street

    if action == "fold":
        p.folded = True
        _record(state, seat, "fold", 0, p.invested_this_street)

    elif action == "check":
        if to_call != 0:
            raise ValueError(f"can't check, {to_call} to call")
        _record(state, seat, "check", 0, p.invested_this_street)

    elif action == "call":
        chips = min(to_call, p.stack)
        if chips == 0:
            raise ValueError("nothing to call; use check")
        _move_chips(state, p, chips)
        _record(state, seat, "call", chips, p.invested_this_street)

    elif action in ("bet", "raise"):
        # `amount` is the TOTAL invested_this_street the player wants to reach
        target = amount
        if target <= state.current_bet and state.current_bet > 0:
            raise ValueError("raise must exceed current bet")
        if state.current_bet == 0 and target < state.big_blind:
            # allow only if all-in for less
            if target != p.invested_this_street + p.stack:
                raise ValueError(f"min bet is {state.big_blind}")
        else:
            min_raise_to = state.current_bet + max(state.last_raise_size, state.big_blind)
            if target < min_raise_to:
                # allow all-in shorts
                if target != p.invested_this_street + p.stack:
                    raise ValueError(f"min raise is to {min_raise_to}")
        cap = p.invested_this_street + p.stack
        if target > cap:
            raise ValueError("not enough chips")
        chips = target - p.invested_this_street
        raise_increment = target - state.current_bet
        _move_chips(state, p, chips)
        if raise_increment >= state.last_raise_size:
            state.last_raise_size = raise_increment
        state.current_bet = target
        state.last_aggressor = seat
        # a raise re-opens action — others must respond again
        state.acted_this_street = {seat}
        _record(state, seat, action, chips, p.invested_this_street)
    else:
        raise ValueError(f"unknown action {action!r}")

    state.acted_this_street.add(seat)
    _advance_turn(state)


def _move_chips(state: GameState, p: Player, chips: int) -> None:
    p.stack -= chips
    p.invested_this_street += chips
    p.invested_total += chips
    state.pot += chips
    if p.stack == 0:
        p.all_in = True


def _record(state: GameState, seat: int, action: str, amount: int, to_amount: int) -> None:
    p = state.player(seat)
    state.history.append(ActionRecord(
        seat=seat, name=p.name, street=state.street,
        action=action, amount=amount, to_amount=to_amount,
        pot_after=state.pot,
    ))


def _advance_turn(state: GameState) -> None:
    """Decide next state: keep betting, advance street, or end hand."""
    # 1) only one player left? hand over
    not_folded = [i for i, p in enumerate(state.players) if not p.folded]
    if len(not_folded) == 1:
        _award_uncontested(state, not_folded[0])
        return

    # 2) all remaining are all-in (or only one can still act and is matched)
    can_act = state.can_act_seats()
    if len(can_act) <= 1 and _street_settled(state):
        # run out the board with no more betting
        _runout_to_showdown(state)
        return

    # 3) is current betting round complete?
    if _street_settled(state):
        _next_street(state)
        return

    # 4) move to next eligible seat
    n = len(state.players)
    seat = (state.to_act + 1) % n
    while True:
        p = state.players[seat]
        if not p.folded and not p.all_in:
            break
        seat = (seat + 1) % n
    state.to_act = seat


def _street_settled(state: GameState) -> bool:
    """Betting round done when every non-folded, non-all-in player has acted
    at the current bet level and matched it."""
    for i, p in enumerate(state.players):
        if p.folded or p.all_in:
            continue
        if p.invested_this_street != state.current_bet:
            return False
        if i not in state.acted_this_street:
            return False
    return True


def _next_street(state: GameState) -> None:
    # collect this street's invested into the pot — already in pot, just reset trackers
    for p in state.players:
        p.invested_this_street = 0
    state.current_bet = 0
    state.last_raise_size = state.big_blind
    state.acted_this_street = set()
    state.last_aggressor = None

    if state.street == Street.PREFLOP:
        # burn 1, deal flop
        state.deck.pop()
        state.board.extend([state.deck.pop(), state.deck.pop(), state.deck.pop()])
        state.street = Street.FLOP
    elif state.street == Street.FLOP:
        state.deck.pop()
        state.board.append(state.deck.pop())
        state.street = Street.TURN
    elif state.street == Street.TURN:
        state.deck.pop()
        state.board.append(state.deck.pop())
        state.street = Street.RIVER
    elif state.street == Street.RIVER:
        _go_to_showdown(state)
        return

    # first to act post-flop = first non-folded, non-all-in seat left of button
    n = len(state.players)
    seat = (state.button + 1) % n
    while True:
        p = state.players[seat]
        if not p.folded and not p.all_in:
            break
        seat = (seat + 1) % n
        if seat == state.button:
            # everyone all-in: just run out
            _runout_to_showdown(state)
            return
    state.to_act = seat


def _runout_to_showdown(state: GameState) -> None:
    """No more betting possible — deal remaining board and go to showdown."""
    # reset street trackers
    for p in state.players:
        p.invested_this_street = 0
    state.current_bet = 0

    while state.street != Street.RIVER:
        if state.street == Street.PREFLOP:
            state.deck.pop()
            state.board.extend([state.deck.pop(), state.deck.pop(), state.deck.pop()])
            state.street = Street.FLOP
        elif state.street == Street.FLOP:
            state.deck.pop()
            state.board.append(state.deck.pop())
            state.street = Street.TURN
        elif state.street == Street.TURN:
            state.deck.pop()
            state.board.append(state.deck.pop())
            state.street = Street.RIVER
    _go_to_showdown(state)


def _award_uncontested(state: GameState, winner_seat: int) -> None:
    p = state.player(winner_seat)
    p.stack += state.pot
    state.winners = [{
        "seats": [winner_seat],
        "amount": state.pot,
        "reason": f"{p.name} wins ${state.pot} (others folded)",
    }]
    state.pot = 0
    state.street = Street.HAND_OVER


def _go_to_showdown(state: GameState) -> None:
    state.street = Street.SHOWDOWN
    contenders = [i for i, p in enumerate(state.players) if not p.folded]
    # reveal everyone's cards
    state.revealed_cards = {
        i: [str(c) for c in state.players[i].hole_cards] for i in contenders
    }

    # build side pots based on invested_total
    invests = sorted({state.players[i].invested_total for i in contenders})
    pots: List[Tuple[int, List[int]]] = []
    prev = 0
    for level in invests:
        eligible = [i for i in contenders if state.players[i].invested_total >= level]
        # all players who put in at least `level` chips (folded too) contribute (level - prev)
        contribution = 0
        for j, p in enumerate(state.players):
            put = min(p.invested_total, level) - min(p.invested_total, prev)
            contribution += put
        pots.append((contribution, eligible))
        prev = level

    # award each pot
    state.winners = []
    for amount, eligible in pots:
        if amount == 0:
            continue
        # rank each eligible player's best 5
        ranked = []
        for s in eligible:
            cards = state.players[s].hole_cards + state.board
            rank, _ = best_hand(cards)
            ranked.append((rank, s))
        ranked.sort(key=lambda x: x[0], reverse=True)
        top = ranked[0][0]
        winners = [s for r, s in ranked if r == top]
        share, remainder = divmod(amount, len(winners))
        for w in winners:
            state.players[w].stack += share
        # leftover chip to first winner left of button
        if remainder:
            order = sorted(winners, key=lambda s: ((s - state.button - 1) % len(state.players)))
            state.players[order[0]].stack += remainder
        rank_name = HAND_NAME[top[0]]
        names = ", ".join(state.players[w].name for w in winners)
        state.winners.append({
            "seats": winners,
            "amount": amount,
            "reason": f"{names} wins ${amount} with {rank_name}",
        })
    state.pot = 0
    state.street = Street.HAND_OVER


# ---------- view projection ----------

def view_for(state: GameState, seat: Optional[int]) -> dict:
    """Render state as a JSON-safe dict, hiding hole cards the seat shouldn't see.

    seat = None means "spectator / hand over reveal everything".
    """
    reveal_all = state.street == Street.HAND_OVER and state.revealed_cards
    players_view = []
    for i, p in enumerate(state.players):
        show_cards = (i == seat) or reveal_all and (i in state.revealed_cards)
        players_view.append({
            "seat": i,
            "name": p.name,
            "is_human": p.is_human,
            "personality": p.personality,
            "stack": p.stack,
            "folded": p.folded,
            "all_in": p.all_in,
            "invested_this_street": p.invested_this_street,
            "invested_total": p.invested_total,
            "hole_cards": [str(c) for c in p.hole_cards] if show_cards else None,
            "is_button": i == state.button,
            "is_to_act": i == state.to_act and state.street not in (Street.SHOWDOWN, Street.HAND_OVER),
        })
    return {
        "hand_number": state.hand_number,
        "players": players_view,
        "button": state.button,
        "blinds": [state.small_blind, state.big_blind],
        "board": [str(c) for c in state.board],
        "pot": state.pot,
        "street": state.street.value,
        "current_bet": state.current_bet,
        "to_act": state.to_act if state.street not in (Street.SHOWDOWN, Street.HAND_OVER) else None,
        "history": [
            {
                "seat": h.seat, "name": h.name, "street": h.street.value,
                "action": h.action, "amount": h.amount, "to_amount": h.to_amount,
                "pot_after": h.pot_after,
            }
            for h in state.history
        ],
        "winners": state.winners,
        "coach_note": state.coach_note,
    }
