"""Per-hand stats inference.

Given a finished GameState, walk the action history and produce each player's
contribution to the standard tracking stats. The web layer accumulates these
into session totals.

All stats are tracked as (numerator, denominator) pairs so they compose
across hands by simple addition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from game import GameState, Street


@dataclass
class PlayerHandStats:
    """One hand's contribution to a single player's running totals."""
    seat: int
    name: str
    personality: str  # "human", "nit", "gto", etc

    # counts (numerator/denominator pairs sum across hands)
    hands_dealt: int = 0          # 1 if dealt cards, 0 if sitting out
    vpip_num: int = 0             # voluntarily put $ in preflop
    pfr_num: int = 0              # was a preflop raiser
    three_bet_num: int = 0
    three_bet_opp: int = 0        # had a chance to 3bet (faced an open before any 3bet, hadn't acted voluntarily yet)
    faced_three_bet: int = 0
    folded_to_three_bet: int = 0
    cbet_flop_num: int = 0
    cbet_flop_opp: int = 0        # was last preflop aggressor + saw flop
    saw_flop: int = 0
    went_to_showdown: int = 0
    chips_won: int = 0            # +/- chips this hand (final stack delta)


def compute_hand_stats(state: GameState, starting_stacks: List[int]) -> List[PlayerHandStats]:
    """Walk the history of a finished hand and produce per-player stats.

    starting_stacks[i] = chips player i had at the start of the hand (before
    blinds). Used to compute chips_won.
    """
    n = len(state.players)
    out = [
        PlayerHandStats(
            seat=i,
            name=state.players[i].name,
            personality=state.players[i].personality or "human",
        )
        for i in range(n)
    ]

    # everyone dealt-in and chips delta
    for i, p in enumerate(state.players):
        out[i].hands_dealt = 1
        out[i].chips_won = p.stack - starting_stacks[i]

    # ------ preflop pass: VPIP, PFR, 3Bet ------
    pf_actions = [h for h in state.history if h.street == Street.PREFLOP and not h.action.startswith("post-")]

    # detect: who was first voluntary aggressor (open raiser),
    # then who 3bet (next raise after an open).
    first_raiser = None  # seat
    second_raiser = None  # 3bet
    raise_count_pf = 0
    seats_facing_open_before_acting: set = set()  # seats that had a chance to 3bet but didn't yet

    # Walk preflop actions in order
    voluntarily_acted: set = set()
    for h in pf_actions:
        seat = h.seat
        # before this seat acts, did they face an open raise (and no 3bet yet)?
        if first_raiser is not None and second_raiser is None and seat not in voluntarily_acted and seat != first_raiser:
            out[seat].three_bet_opp += 1

        if h.action == "fold":
            voluntarily_acted.add(seat)
            # facing 3bet?
            if second_raiser is not None and seat != second_raiser:
                if seat == first_raiser:
                    out[seat].folded_to_three_bet += 1
            continue

        # check is voluntary but doesn't put $ in (only the BB checking option preflop)
        # call/bet/raise = puts chips in voluntarily
        if h.action in ("call", "bet", "raise"):
            out[seat].vpip_num = 1  # max once per hand
        voluntarily_acted.add(seat)

        if h.action in ("bet", "raise"):
            raise_count_pf += 1
            out[seat].pfr_num = 1
            if first_raiser is None:
                first_raiser = seat
            elif second_raiser is None:
                second_raiser = seat
                out[seat].three_bet_num = 1
                # the original raiser is now facing a 3bet
                if first_raiser is not None:
                    out[first_raiser].faced_three_bet = 1
                # also any other player who already called the open and now faces a 3bet
                # (we don't track squeeze separately here)

    # ------ flop pass: c-bet & saw flop ------
    if any(h.street == Street.FLOP for h in state.history) or state.board:
        # seats that didn't fold preflop saw the flop
        # however our state already has folded set — but we want "saw flop in this hand"
        # easier: any non-folded player at start-of-flop who isn't all-in-without-flop
        # Simpler approximation: anyone whose last preflop action wasn't fold and they have hole cards
        non_folded_pf = set(range(n))
        for h in pf_actions:
            if h.action == "fold":
                non_folded_pf.discard(h.seat)
        for s in non_folded_pf:
            out[s].saw_flop = 1

        # c-bet opportunity: last preflop aggressor (if exists) + saw flop
        last_pf_aggressor = second_raiser if second_raiser is not None else first_raiser
        if last_pf_aggressor is not None and last_pf_aggressor in non_folded_pf:
            out[last_pf_aggressor].cbet_flop_opp = 1
            # did they bet/raise on the flop as the first action?
            flop_actions = [h for h in state.history if h.street == Street.FLOP]
            for h in flop_actions:
                if h.seat == last_pf_aggressor:
                    if h.action in ("bet", "raise"):
                        out[last_pf_aggressor].cbet_flop_num = 1
                    break  # only first action by them counts
                if h.action in ("bet", "raise"):
                    # someone donk-bet into them; c-bet opp lost
                    break

    # ------ showdown ------
    if state.revealed_cards:
        for seat in state.revealed_cards.keys():
            out[seat].went_to_showdown = 1

    return out


@dataclass
class CumulativeStats:
    """Session-level totals for a single player. All fields are cumulative
    counters; percentages are derived on read."""
    name: str
    personality: str
    hands: int = 0
    vpip_num: int = 0
    pfr_num: int = 0
    three_bet_num: int = 0
    three_bet_opp: int = 0
    faced_three_bet: int = 0
    folded_to_three_bet: int = 0
    cbet_flop_num: int = 0
    cbet_flop_opp: int = 0
    saw_flop: int = 0
    went_to_showdown: int = 0
    chips_won: int = 0  # cumulative chip delta from starting stacks across hands

    def add(self, h: PlayerHandStats) -> None:
        self.hands += h.hands_dealt
        self.vpip_num += h.vpip_num
        self.pfr_num += h.pfr_num
        self.three_bet_num += h.three_bet_num
        self.three_bet_opp += h.three_bet_opp
        self.faced_three_bet += h.faced_three_bet
        self.folded_to_three_bet += h.folded_to_three_bet
        self.cbet_flop_num += h.cbet_flop_num
        self.cbet_flop_opp += h.cbet_flop_opp
        self.saw_flop += h.saw_flop
        self.went_to_showdown += h.went_to_showdown
        self.chips_won += h.chips_won

    def to_dict(self, big_blind: int = 2) -> dict:
        def pct(num: int, den: int) -> float | None:
            if den == 0:
                return None
            return round(100.0 * num / den, 1)
        bb_per_100 = None
        if self.hands > 0 and big_blind > 0:
            bb_per_100 = round(100.0 * self.chips_won / (self.hands * big_blind), 1)
        return {
            "name": self.name,
            "personality": self.personality,
            "hands": self.hands,
            "chips_won": self.chips_won,
            "bb_per_100": bb_per_100,
            "vpip": pct(self.vpip_num, self.hands),
            "pfr": pct(self.pfr_num, self.hands),
            "three_bet": pct(self.three_bet_num, self.three_bet_opp),
            "fold_to_three_bet": pct(self.folded_to_three_bet, self.faced_three_bet),
            "cbet_flop": pct(self.cbet_flop_num, self.cbet_flop_opp),
            "wtsd": pct(self.went_to_showdown, self.saw_flop),
            "raw": {
                "vpip_num": self.vpip_num,
                "pfr_num": self.pfr_num,
                "three_bet_num": self.three_bet_num,
                "three_bet_opp": self.three_bet_opp,
                "faced_three_bet": self.faced_three_bet,
                "folded_to_three_bet": self.folded_to_three_bet,
                "cbet_flop_num": self.cbet_flop_num,
                "cbet_flop_opp": self.cbet_flop_opp,
                "saw_flop": self.saw_flop,
                "went_to_showdown": self.went_to_showdown,
            },
        }


class StatsTracker:
    """Session container — tracks cumulative stats keyed by player name.

    We key by name (not seat) because seats rotate, but Stone is always Stone.
    Also keeps a per-hand timeline for charting.
    """
    def __init__(self) -> None:
        self.players: Dict[str, CumulativeStats] = {}
        # timeline: per-hand snapshot of every player. Each entry is
        # {"hand": int, "snapshots": {name -> {chips_won, vpip, pfr, ...}}}
        # The student (human) timeline is what the chart uses, but we store all.
        self.timeline: List[dict] = []

    def record_hand(self, hand_stats: List[PlayerHandStats]) -> None:
        for h in hand_stats:
            cum = self.players.setdefault(
                h.name, CumulativeStats(name=h.name, personality=h.personality)
            )
            cum.add(h)
        # take a snapshot now (after applying this hand)
        snap = {}
        for name, cum in self.players.items():
            snap[name] = {
                "chips_won": cum.chips_won,
                "vpip": _safe_pct(cum.vpip_num, cum.hands),
                "pfr": _safe_pct(cum.pfr_num, cum.hands),
                "wtsd": _safe_pct(cum.went_to_showdown, cum.saw_flop),
            }
        hand_idx = max((cum.hands for cum in self.players.values()), default=0)
        self.timeline.append({"hand": hand_idx, "snapshots": snap})

    def reset(self) -> None:
        self.players.clear()
        self.timeline.clear()

    def snapshot(self, big_blind: int = 2) -> dict:
        return {
            "players": [self.players[n].to_dict(big_blind) for n in sorted(self.players)],
            "timeline": self.timeline,
        }


def _safe_pct(num: int, den: int) -> float | None:
    if den == 0:
        return None
    return round(100.0 * num / den, 1)
