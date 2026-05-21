"""Turns game state into an opponent action via the configured LLM provider.

Each call is independent — opponents can't see each other's reasoning or
hole cards. The actual API call goes through llm.chat(), which dispatches
to Anthropic or OpenAI based on the LLM_PROVIDER env var.
"""
from __future__ import annotations

import json
import re

from game import GameState, HAND_NAME, Street, best_hand, legal_actions
from llm import chat as llm_chat
from personalities import build_system_prompt


def build_situation_prompt(state: GameState, seat: int) -> str:
    """Render the game state from `seat`'s point of view as a user message."""
    p = state.player(seat)
    n = len(state.players)

    lines = []
    lines.append(f"=== HAND #{state.hand_number} | STREET: {state.street.value.upper()} ===")
    lines.append(f"Blinds: ${state.small_blind}/${state.big_blind}, 3-handed cash game.")
    lines.append("")

    # seats around the table, starting from button
    lines.append("Players (seat / name / stack / status):")
    for i in range(n):
        seat_idx = (state.button + i) % n
        pl = state.players[seat_idx]
        tag = []
        if seat_idx == state.button:
            tag.append("BTN")
        # blind tags depend on heads-up or 3+ handed
        active = [j for j, q in enumerate(state.players) if q.stack > 0 or q.invested_total > 0]
        if len(active) == 2:
            if seat_idx == state.button:
                tag.append("SB")
            else:
                tag.append("BB")
        else:
            if seat_idx == (state.button + 1) % n:
                tag.append("SB")
            elif seat_idx == (state.button + 2) % n:
                tag.append("BB")
        if pl.folded:
            tag.append("FOLDED")
        if pl.all_in:
            tag.append("ALL-IN")
        if seat_idx == seat:
            tag.append("YOU")
        lines.append(f"  Seat {seat_idx} {pl.name} (${pl.stack}) [{'/'.join(tag)}]")

    lines.append("")
    board = " ".join(str(c) for c in state.board) or "(none yet)"
    lines.append(f"Board: {board}")
    lines.append(f"Pot: ${state.pot}")
    lines.append(f"Current bet to match this street: ${state.current_bet}")
    lines.append("")
    lines.append(f"Your hole cards: {' '.join(str(c) for c in p.hole_cards)}")
    lines.append(f"Your stack: ${p.stack}")
    lines.append(f"You've put ${p.invested_this_street} into this betting round so far.")
    to_call = state.current_bet - p.invested_this_street
    lines.append(f"To call: ${to_call}")

    lines.append("")
    lines.append("Action history this hand:")
    if not state.history:
        lines.append("  (none yet)")
    else:
        last_street = None
        for h in state.history:
            if h.street != last_street:
                lines.append(f"  -- {h.street.value} --")
                last_street = h.street
            extra = ""
            if h.action in ("call", "bet", "raise", "post-sb", "post-bb"):
                extra = f" ${h.amount} (to ${h.to_amount}, pot ${h.pot_after})"
            lines.append(f"  {h.name}: {h.action}{extra}")

    lines.append("")
    legal = legal_actions(state, seat)
    lines.append("Legal actions right now:")
    for a in legal["actions"]:
        if a["type"] in ("fold", "check"):
            lines.append(f"  - {a['type']}")
        elif a["type"] == "call":
            lines.append(f"  - call ${a['amount']}")
        elif a["type"] == "bet":
            lines.append(f"  - bet (min ${a['min']}, max ${a['max']}, amount = total invested_this_street to reach)")
        elif a["type"] == "raise":
            lines.append(f"  - raise (min to ${a['min_to']}, max to ${a['max_to']}, amount = total invested_this_street)")

    lines.append("")
    lines.append("Decide your action now.")
    return "\n".join(lines)


def _parse_action_json(text: str) -> dict:
    """Pull the first JSON object out of the model's response."""
    # try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # find first {...} block
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON found in model response: {text!r}")
    return json.loads(m.group(0))


def decide(state: GameState, seat: int) -> dict:
    """Ask the AI for a decision. Returns {action, amount, reasoning}."""
    p = state.player(seat)
    if not p.personality:
        raise ValueError(f"seat {seat} has no personality")
    system = build_system_prompt(p.personality)
    user = build_situation_prompt(state, seat)

    text = llm_chat(system=system, user=user, max_tokens=300)
    parsed = _parse_action_json(text)

    # normalize
    action = str(parsed.get("action", "")).lower().strip()
    amount = int(parsed.get("amount", 0) or 0)
    reasoning = str(parsed.get("reasoning", "") or "").strip()

    # safety: clamp / fix obvious mistakes
    legal = legal_actions(state, seat)
    types = {a["type"] for a in legal["actions"]}
    if action == "all-in" or action == "allin":
        # treat all-in as raise to max
        action = "raise" if state.current_bet > 0 else "bet"
        amount = p.invested_this_street + p.stack
    if action not in types:
        # fallback: check if free, else fold
        if "check" in types:
            action, amount = "check", 0
        elif "call" in types:
            ca = next(a for a in legal["actions"] if a["type"] == "call")
            action, amount = "call", ca["amount"]
        else:
            action, amount = "fold", 0
    return {
        "action": action,
        "amount": amount,
        "reasoning": reasoning or "(silent)",
    }


def coach_review(state: GameState, human_seat: int) -> str:
    """Generate a short coaching note about the hand from a GTO/coach view.

    Independent of the playing personalities — uses a fresh call dedicated
    to teaching the human player.
    """
    coach_system = """
你是一位资深 NLHE 德州扑克教练，正在复盘学生刚打完的一手牌。
要求具体、聚焦——只挑这手中学生最关键的一个决策点深入讲，不要面面俱到。

重要：用户消息里会显式给出每位玩家的"最终成牌等级"和组成的 5 张牌。这是引擎算好的 ground truth，
直接采信，不要自己重新评估手牌等级（这是过去常出错的地方）。

【按场景调整分析重心】
- 走到摊牌：重心放在 hand strength / value betting / bluff-catching / sizing 等 postflop 主题。
- 没到摊牌（uncontested 或对手中途弃牌）：对手底牌不可见，不要瞎猜对手具体牌；重心放在
  preflop range / position / steal 频率 / 3-bet defend / fold equity / c-bet 偷池频率
  / 弃牌纪律 等概念性主题。
- preflop 就结束的局：重心放在起手牌选择、位置利用、对手画像（Nit/GTO/LAG/Station）下
  的偷盲 vs 防守决策。

输出格式（中文叙述，德扑术语保留英文：c-bet / equity / range / pot odds / polarized / TPTK / set / board texture / value / bluff / bluff-catcher / 3-bet / GTO 等）:

【关键决策】<街+动作+利害，1 句话>
【分析】<2-3 句。结合学生手牌、对手 range、pot odds、board texture 等给出依据>
【判定】<"打得好" | "可以接受，但..." | "明显漏洞"> —— 一句话说原因
【建议】<如果不是"打得好"，给一个具体可执行的改进；否则写"继续保持">

整体不超过 200 字。
""".strip()

    n = len(state.players)
    you = state.players[human_seat]
    pers_label = {"nit": "Nit 极紧", "gto": "GTO 平衡", "lag": "LAG 松凶", "station": "Station 跟注站"}

    # 怎么结束的：摊牌 / 对手 preflop 弃 / 对手中途弃
    went_to_showdown = bool(state.revealed_cards)
    saw_flop = len(state.board) >= 3
    if went_to_showdown:
        ending = "走到摊牌"
    elif saw_flop:
        ending = "未到摊牌（对手在 flop/turn/river 弃牌）"
    else:
        ending = "preflop 就结束（对手 preflop 弃牌或者你 fold 了）"

    lines = [f"3-handed NLHE，盲注 ${state.small_blind}/${state.big_blind}，100bb 有效筹码。"]
    lines.append(f"学生是 {you.name}（座位 {human_seat}）。")
    lines.append(f"本手结束方式：{ending}。")
    lines.append("")
    lines.append("最终状态：")
    lines.append(f"  公共牌：{' '.join(str(c) for c in state.board) or '（未到翻牌）'}")

    # 对手画像（无论是否摊牌都列，便于教练讨论 range）
    lines.append("  桌上玩家：")
    for i, pl in enumerate(state.players):
        if i == human_seat:
            continue
        label = pers_label.get(pl.personality, "人类玩家")
        lines.append(f"    座位 {i} {pl.name}（{label}）")

    if went_to_showdown:
        lines.append("")
        lines.append("  摊牌时亮出的底牌（已由引擎评估好成牌等级，不要重新猜）：")
        for s, cards in state.revealed_cards.items():
            pl = state.players[s]
            label = pers_label.get(pl.personality, "人类玩家")
            all_cards = pl.hole_cards + state.board
            if len(all_cards) >= 5:
                rank_tuple, best_5 = best_hand(all_cards)
                cat = HAND_NAME[rank_tuple[0]]
                best_str = " ".join(str(c) for c in best_5)
                lines.append(
                    f"    座位 {s} {pl.name}（{label}）：底牌 {' '.join(cards)} → 最终成牌 = {cat} [{best_str}]"
                )
            else:
                lines.append(f"    座位 {s} {pl.name}（{label}）：{' '.join(cards)}")
    else:
        lines.append("")
        lines.append("  对手底牌：不可见（未到摊牌）。请基于其 personality / 位置 / 行动模式估计 range，"
                     "不要瞎猜对手的具体两张牌。")
    lines.append("")
    # student final hand category as ground truth
    if len(you.hole_cards) >= 2 and len(you.hole_cards) + len(state.board) >= 5:
        rank_tuple, best_5 = best_hand(you.hole_cards + state.board)
        cat = HAND_NAME[rank_tuple[0]]
        best_str = " ".join(str(c) for c in best_5)
        lines.append(f"学生底牌：{' '.join(str(c) for c in you.hole_cards)} → 最终成牌 = {cat} [{best_str}]")
    else:
        lines.append(f"学生底牌：{' '.join(str(c) for c in you.hole_cards)}")
    lines.append("")
    lines.append("行动历史：")
    last_street = None
    for h in state.history:
        if h.street != last_street:
            lines.append(f"  —— {h.street.value} ——")
            last_street = h.street
        extra = ""
        if h.action in ("call", "bet", "raise", "post-sb", "post-bb"):
            extra = f" ${h.amount}（累计 ${h.to_amount}）"
        lines.append(f"  {h.name}：{h.action}{extra}")
    lines.append("")
    lines.append("赢家：")
    for w in state.winners:
        lines.append(f"  {w['reason']}")
    lines.append("")
    lines.append("请聚焦学生这手最关键的决策点，给出针对性复盘。")

    return llm_chat(
        system=coach_system,
        user="\n".join(lines),
        max_tokens=600,
    ).strip()
