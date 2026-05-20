// ===== state =====
let state = null;
let aiLoopBusy = false;
let lastAiSeenLogIndex = -1;

// ===== utility =====
const SUIT_GLYPH = { s: "♠", h: "♥", d: "♦", c: "♣" };
const SUIT_COLOR = { s: "black", c: "black", h: "red", d: "red" };

function cardEl(cardStr, { community = false } = {}) {
  const div = document.createElement("div");
  div.className = "card";
  if (community) div.classList.add("community-card");
  if (!cardStr) {
    div.classList.add("back");
    return div;
  }
  const rank = cardStr[0];
  const suit = cardStr[1];
  div.classList.add(SUIT_COLOR[suit]);
  div.innerHTML = `
    <div class="rank">${rank === "T" ? "10" : rank}</div>
    <div class="suit">${SUIT_GLYPH[suit]}</div>
    <div class="rank-bottom">${rank === "T" ? "10" : rank}</div>
  `;
  return div;
}

function miniCard(cardStr) {
  const c = cardEl(cardStr);
  c.style.width = "30px";
  c.style.height = "42px";
  return c;
}

function $(sel, root = document) { return root.querySelector(sel); }
function $$(sel, root = document) { return [...root.querySelectorAll(sel)]; }

// ===== render =====
function render() {
  if (!state) return;

  $("#hand-num").textContent = `#${state.hand_number}`;
  $("#pot-amount").textContent = `$${state.pot}`;
  $("#street-label").textContent = state.street.toUpperCase();

  // community cards
  const community = $("#community");
  community.innerHTML = "";
  state.board.forEach(c => community.appendChild(cardEl(c, { community: true })));
  // pad with backs to keep layout stable when fewer than 5
  for (let i = state.board.length; i < 5; i++) {
    const ph = document.createElement("div");
    ph.className = "card community-card";
    ph.style.visibility = "hidden";
    community.appendChild(ph);
  }

  // seats
  state.players.forEach(p => {
    const seat = $(`#seat-${p.seat}`);
    if (!seat) return;
    seat.classList.toggle("to-act", p.is_to_act);
    seat.classList.toggle("folded", p.folded);

    const persTag = p.personality ? `<span class="pers-tag">${p.personality}</span>` : "";
    $(".player-name", seat).innerHTML = `${p.name}${persTag}`;
    $(".player-stack", seat).textContent = `$${p.stack}` + (p.all_in ? " · 全下" : "");

    const cardsBox = $(".player-cards", seat);
    cardsBox.innerHTML = "";
    if (p.folded) {
      // no cards shown
    } else if (p.hole_cards) {
      p.hole_cards.forEach(c => cardsBox.appendChild(cardEl(c)));
    } else {
      // hidden — show backs
      cardsBox.appendChild(cardEl(null));
      cardsBox.appendChild(cardEl(null));
    }

    const betBox = $(".player-bet", seat);
    if (p.invested_this_street > 0) {
      betBox.textContent = `$${p.invested_this_street}`;
      betBox.classList.add("has-bet");
    } else {
      betBox.classList.remove("has-bet");
    }

    $(".dealer-button", seat).classList.toggle("hidden", !p.is_button);
  });

  // action log
  const log = $("#action-log");
  log.innerHTML = "";
  let lastStreet = null;
  state.history.forEach(h => {
    if (h.street !== lastStreet) {
      const div = document.createElement("div");
      div.className = "street-divider";
      div.textContent = STREET_CN[h.street] || h.street;
      log.appendChild(div);
      lastStreet = h.street;
    }
    const e = document.createElement("div");
    e.className = "entry";
    let amt = "";
    if (["call", "bet", "raise", "post-sb", "post-bb"].includes(h.action)) {
      amt = `$${h.amount}`;
    }
    e.innerHTML = `<span class="who">${h.name}</span><span class="what">${ACTION_CN[h.action] || h.action}</span><span class="amt">${amt}</span>`;
    log.appendChild(e);
  });
  log.scrollTop = log.scrollHeight;

  // AI reasoning
  const reasoningBox = $("#ai-reasoning");
  reasoningBox.innerHTML = "";
  const reasons = state.ai_reasoning || {};
  Object.keys(reasons).sort((a, b) => +a - +b).forEach(idx => {
    const h = state.history[+idx];
    if (!h) return;
    const div = document.createElement("div");
    div.className = "quote";
    const street = STREET_CN[h.street] || h.street;
    const act = ACTION_CN[h.action] || h.action;
    div.innerHTML = `<span class="who">${h.name}</span> · ${street} · ${act}<br>"${reasons[idx]}"`;
    reasoningBox.appendChild(div);
  });
  reasoningBox.scrollTop = reasoningBox.scrollHeight;

  // action bar
  renderActionBar();

  // hand-over: show review panel (左下角常驻面板，取代弹窗)
  if (state.street === "hand_over") {
    updateReviewPanel();
    $("#btn-next-hand").disabled = false;
  } else {
    hideReviewPanel();
    $("#btn-next-hand").disabled = true;
  }
}

const STREET_CN = {
  preflop: "翻牌前",
  flop: "翻牌",
  turn: "转牌",
  river: "河牌",
  showdown: "摊牌",
  hand_over: "结束",
};
const ACTION_CN = {
  fold: "弃牌",
  check: "过牌",
  call: "跟注",
  bet: "下注",
  raise: "加注",
  "post-sb": "下小盲",
  "post-bb": "下大盲",
};

function renderActionBar() {
  const bar = $("#action-bar");
  const buttons = $$("#action-bar .action-buttons button");
  buttons.forEach(b => b.disabled = true);
  $(".raise-controls").classList.add("hidden");

  const legal = state.your_legal_actions;
  if (!legal || state.street === "hand_over" || state.street === "showdown") {
    bar.classList.add("disabled");
    return;
  }
  bar.classList.remove("disabled");

  const types = {};
  legal.actions.forEach(a => types[a.type] = a);

  // fold
  if (types.fold) $("[data-action='fold']").disabled = false;

  // check vs call
  if (types.check) {
    const btn = $("[data-action='check']");
    btn.disabled = false;
    btn.classList.remove("hidden");
    $("[data-action='call']").classList.add("hidden");
  } else {
    $("[data-action='check']").classList.add("hidden");
    if (types.call) {
      const btn = $("[data-action='call']");
      btn.disabled = false;
      btn.classList.remove("hidden");
      $(".sub-amt", btn).textContent = `$${types.call.amount}`;
    }
  }

  // bet vs raise
  const raiseBtn = $("[data-action='raise']");
  if (types.bet) {
    raiseBtn.disabled = false;
    raiseBtn.firstChild.textContent = "Bet";
    $(".sub-amt", raiseBtn).textContent = "";
    raiseBtn.dataset.minTo = types.bet.min;
    raiseBtn.dataset.maxTo = types.bet.max;
  } else if (types.raise) {
    raiseBtn.disabled = false;
    raiseBtn.firstChild.textContent = "Raise";
    $(".sub-amt", raiseBtn).textContent = `to $${types.raise.min_to}+`;
    raiseBtn.dataset.minTo = types.raise.min_to;
    raiseBtn.dataset.maxTo = types.raise.max_to;
  }
}

// ===== review panel (左下角常驻) =====
let _lastReviewedHand = -1;

function updateReviewPanel() {
  const panel = $("#review-panel");
  panel.classList.remove("hidden");
  // 新一手结束时重置教练区，并保持展开
  const isNewHand = state.hand_number !== _lastReviewedHand;
  if (isNewHand) {
    _lastReviewedHand = state.hand_number;
    panel.classList.remove("collapsed");
    $("#review-toggle").textContent = "−";
    const note = $("#coach-note");
    note.textContent = "";
    note.classList.add("hidden");
    $("#btn-coach").disabled = false;
    $("#btn-coach").textContent = "请教练复盘";
  }

  $("#review-title-text").textContent = `第 ${state.hand_number} 手 · 已结束`;

  const reveal = $("#reveal-cards");
  reveal.innerHTML = "";
  let anyShown = false;
  state.players.forEach(p => {
    if (p.folded || !p.hole_cards) return;
    anyShown = true;
    const row = document.createElement("div");
    row.className = "reveal-row";
    const nm = document.createElement("div");
    nm.className = "name";
    nm.textContent = p.name;
    const cards = document.createElement("div");
    cards.className = "mini-cards";
    p.hole_cards.forEach(c => cards.appendChild(cardEl(c)));
    row.append(nm, cards);
    reveal.appendChild(row);
  });
  if (!anyShown) {
    reveal.innerHTML = `<div style="font-size:11px;color:var(--text-dim);">对手 preflop 弃牌，无摊牌</div>`;
  }

  const w = $("#winners-list");
  w.innerHTML = (state.winners || []).map(x => `🏆 ${x.reason}`).join("<br>");
}

function hideReviewPanel() {
  $("#review-panel").classList.add("hidden");
}

function toggleReviewPanel() {
  const panel = $("#review-panel");
  panel.classList.toggle("collapsed");
  $("#review-toggle").textContent = panel.classList.contains("collapsed") ? "+" : "−";
}

// ===== stats modal =====
async function openStats() {
  const modal = $("#stats-modal");
  modal.classList.remove("hidden");
  const body = $("#stats-body");
  body.innerHTML = "<div style='color:var(--text-dim);font-size:13px;'>加载中…</div>";
  try {
    // 先拉 sessions 填下拉框，再拉默认 session 的 stats
    await populateSessionSelect();
    await loadStatsForSelected();
  } catch (e) {
    body.innerHTML = `<div style="color:var(--danger);">加载失败: ${e.message}</div>`;
  }
}

async function populateSessionSelect() {
  const sel = $("#session-select");
  const data = await api("/api/sessions");
  const sessions = data.sessions || [];
  const current = data.current;
  sel.innerHTML = "";

  if (sessions.length === 0) {
    const opt = document.createElement("option");
    opt.value = ""; opt.textContent = "（尚无对局，先打一手）";
    opt.disabled = true; opt.selected = true;
    sel.appendChild(opt);
    return;
  }

  sessions.forEach(s => {
    const opt = document.createElement("option");
    opt.value = s.id;
    const date = new Date(s.started_at).toLocaleString("zh-CN", { hour12: false });
    const opps = (s.opponents || []).join(" + ");
    const chips = s.you_chips_won;
    const sign = chips > 0 ? "+" : "";
    const isCurrent = s.id === current ? " · 当前" : "";
    opt.textContent = `#${s.id} · ${date} · ${opps} · ${s.hand_count} 手 · ${sign}$${chips}${isCurrent}`;
    if (s.id === current) opt.selected = true;
    sel.appendChild(opt);
  });
  // default to the topmost (most recent) if no current
  if (current == null && sel.options.length > 0) sel.options[0].selected = true;
}

async function loadStatsForSelected() {
  const sel = $("#session-select");
  const sid = sel.value;
  const url = sid ? `/api/stats?session_id=${sid}` : "/api/stats";
  const data = await api(url);
  renderStats(data);
}

// ===== history modal =====
async function openHistory() {
  const modal = $("#history-modal");
  modal.classList.remove("hidden");
  await renderHistory();
}

async function renderHistory() {
  const list = $("#history-list");
  list.innerHTML = `<div class="chart-empty">加载中…</div>`;
  let data;
  try {
    data = await api("/api/sessions");
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger);font-size:13px;padding:20px;">加载失败: ${e.message}</div>`;
    return;
  }
  const sessions = data.sessions || [];
  const current = data.current;

  if (sessions.length === 0) {
    list.innerHTML = `<div class="chart-empty">还没有历史会话。点「重开一局」开始第一局。</div>`;
    return;
  }

  list.innerHTML = "";
  sessions.forEach(s => list.appendChild(renderHistoryCard(s, current)));
}

function renderHistoryCard(s, current) {
  const card = document.createElement("div");
  card.className = "history-card";
  if (s.id === current) card.classList.add("current");
  if (s.hand_count === 0) card.classList.add("empty");

  const stacks = s.last_stacks;
  const allBusted = stacks && Object.values(stacks).every(v => v === 0);
  if (allBusted) card.classList.add("busted");

  // header line
  const date = new Date(s.started_at).toLocaleString("zh-CN", { hour12: false });
  const opps = (s.opponents || []).join(" + ");
  const chips = s.you_chips_won;
  const sign = chips > 0 ? "+" : "";
  const chipsClass = chips > 0 ? "hc-pos" : chips < 0 ? "hc-neg" : "";

  const isCurrent = s.id === current ? `<span class="hc-current-tag">当前</span>` : "";

  let stacksLine = "";
  if (stacks) {
    stacksLine = `<div class="hc-stacks">最后筹码：` +
      Object.entries(stacks).map(([n, v]) => `<span>${n} $${v}</span>`).join("") +
      `</div>`;
  } else {
    stacksLine = `<div class="hc-stacks" style="font-style:italic;">尚未打过手牌</div>`;
  }

  card.innerHTML = `
    <div class="hc-meta">
      <div class="hc-title">
        <span class="hc-id">#${s.id}</span>
        <span>${opps}</span>
        ${isCurrent}
      </div>
      <div class="hc-row">
        ${date} · ${s.hand_count} 手 ·
        <span class="${chipsClass}">${sign}$${chips}</span>
      </div>
      ${stacksLine}
    </div>
    <div class="hc-actions">
      <button class="hc-view ghost">查看数据</button>
      <button class="hc-resume primary" ${allBusted || s.id === current ? "disabled" : ""}>接着玩</button>
      <button class="hc-delete danger" ${s.id === current ? "disabled" : ""}>删除</button>
    </div>
  `;

  card.querySelector(".hc-view").addEventListener("click", () => {
    $("#history-modal").classList.add("hidden");
    openStatsForSession(s.id);
  });
  card.querySelector(".hc-resume").addEventListener("click", () => resumeSession(s));
  card.querySelector(".hc-delete").addEventListener("click", () => deleteSessionFlow(s));

  return card;
}

async function openStatsForSession(sessionId) {
  await openStats();
  const sel = $("#session-select");
  sel.value = String(sessionId);
  await loadStatsForSelected();
}

async function resumeSession(s) {
  const opps = (s.opponents || []).join(" + ");
  const stacksStr = s.last_stacks
    ? Object.entries(s.last_stacks).map(([n, v]) => `${n} $${v}`).join(", ")
    : `各人 $${s.starting_stack}`;
  const ok = confirm(
    `从会话 #${s.id}（${opps}）继续？\n\n下一手起始筹码：\n${stacksStr}\n\n（当前正在打的牌局如果有，会先终止）`
  );
  if (!ok) return;
  try {
    state = await api(`/api/sessions/${s.id}/resume`, { method: "POST" });
    $("#history-modal").classList.add("hidden");
    render();
    driveAI();
  } catch (e) {
    alert("接着玩失败: " + e.message);
  }
}

async function deleteSessionFlow(s) {
  const opps = (s.opponents || []).join(" + ");
  const ok = confirm(
    `删除会话 #${s.id}（${opps}，${s.hand_count} 手）？\n\n这一会话的所有手牌、动作、教练复盘都会被永久删除，不可恢复。`
  );
  if (!ok) return;
  try {
    await api(`/api/sessions/${s.id}`, { method: "DELETE" });
    await renderHistory();
  } catch (e) {
    alert("删除失败: " + e.message);
  }
}

function renderStats(data) {
  const players = data.players || [];
  const meta = $("#stats-meta");
  const charts = $("#stats-charts");
  const body = $("#stats-body");

  if (players.length === 0) {
    meta.textContent = "尚无完成的牌局";
    charts.innerHTML = "";
    body.innerHTML =
      `<div class="chart-empty">打完至少一手再回来看 →</div>`;
    return;
  }
  const totalHands = Math.max(...players.map(p => p.hands));
  meta.textContent = `已完成 ${totalHands} 手`;

  // sort: You first, then by personality
  const order = { human: 0, gto: 1, lag: 2, nit: 3, station: 4 };
  players.sort((a, b) => (order[a.personality] ?? 9) - (order[b.personality] ?? 9));

  // ---- charts (You only) ----
  charts.innerHTML = "";
  const you = players.find(p => p.personality === "human");
  const timeline = data.timeline || [];
  if (you && timeline.length >= 1) {
    charts.appendChild(buildChipsChart(you, timeline));
    charts.appendChild(buildVpipPfrChart(you, timeline));
  } else {
    charts.innerHTML = `<div class="chart-empty" style="grid-column: 1 / -1;">打完至少一手再回来看趋势曲线 →</div>`;
  }

  body.innerHTML = "";
  players.forEach(p => body.appendChild(playerStatsCard(p)));
}

// ---- chart builders ----
function buildChipsChart(you, timeline) {
  const card = document.createElement("div");
  card.className = "chart-card";
  const points = timeline.map(t => ({
    x: t.hand,
    y: (t.snapshots[you.name] || {}).chips_won ?? 0,
  }));
  const cur = you.chips_won;
  const sign = cur > 0 ? "+" : "";
  const cls = cur > 0 ? "pos" : cur < 0 ? "neg" : "";
  card.innerHTML = `
    <div class="chart-title">
      <span>盈亏曲线（累计 $）</span>
      <span class="chart-current ${cls}">${sign}$${cur}</span>
    </div>
    ${renderLineChart(points, { fill: true, baseline: 0, color: "var(--good)", colorNeg: "var(--danger)" })}
  `;
  return card;
}

function buildVpipPfrChart(you, timeline) {
  const card = document.createElement("div");
  card.className = "chart-card";
  const vpipPts = timeline.map(t => ({ x: t.hand, y: (t.snapshots[you.name] || {}).vpip ?? null }));
  const pfrPts  = timeline.map(t => ({ x: t.hand, y: (t.snapshots[you.name] || {}).pfr  ?? null }));
  const curVpip = you.vpip == null ? "—" : `${you.vpip}%`;
  const curPfr  = you.pfr  == null ? "—" : `${you.pfr}%`;
  card.innerHTML = `
    <div class="chart-title">
      <span>风格趋势</span>
      <span class="chart-current">VPIP ${curVpip} · PFR ${curPfr}</span>
    </div>
    ${renderMultiLine(
      [
        { points: vpipPts, color: "#4a90d9" },
        { points: pfrPts,  color: "#d4a857" },
      ],
      { yMin: 0, yMax: 100 }
    )}
    <div class="chart-legend">
      <span class="legend-item"><span class="legend-swatch" style="background:#4a90d9"></span>VPIP</span>
      <span class="legend-item"><span class="legend-swatch" style="background:#d4a857"></span>PFR</span>
    </div>
  `;
  return card;
}

// pure SVG line chart
function renderLineChart(points, opts = {}) {
  if (points.length < 1) return "";
  const W = 400, H = 140, P = 22; // padding
  const xs = points.map(p => p.x);
  const ys = points.map(p => p.y);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = opts.yMin ?? Math.min(...ys, opts.baseline ?? Infinity);
  let yMax = opts.yMax ?? Math.max(...ys, opts.baseline ?? -Infinity);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const xSpan = Math.max(1, xMax - xMin);
  const ySpan = yMax - yMin;
  const xToPx = x => P + ((x - xMin) / xSpan) * (W - 2 * P);
  const yToPx = y => H - P - ((y - yMin) / ySpan) * (H - 2 * P);

  // baseline (zero line)
  const baseY = opts.baseline != null ? yToPx(opts.baseline) : null;

  // path
  let d = "";
  points.forEach((p, i) => {
    d += (i === 0 ? "M" : "L") + xToPx(p.x) + "," + yToPx(p.y) + " ";
  });

  let fill = "";
  if (opts.fill && baseY != null) {
    const start = points[0], end = points[points.length - 1];
    fill = `<path d="M${xToPx(start.x)},${baseY} ${d.replace(/^M/, "L")} L${xToPx(end.x)},${baseY} Z"
                  fill="${opts.color}" opacity="0.12"/>`;
  }
  // determine line color (single segment for now; if user is overall in red, use neg color)
  const last = points[points.length - 1];
  let lineColor = opts.color;
  if (opts.colorNeg && opts.baseline != null && last.y < opts.baseline) {
    lineColor = opts.colorNeg;
  }

  // axis labels
  const yLabels = `
    <text x="4" y="${P + 4}"      font-size="9" fill="#888">${Math.round(yMax)}</text>
    <text x="4" y="${H - P + 3}"  font-size="9" fill="#888">${Math.round(yMin)}</text>
  `;
  const xLabels = `
    <text x="${P}" y="${H - 4}"     font-size="9" fill="#888">手 ${xMin}</text>
    <text x="${W - P - 22}" y="${H - 4}" font-size="9" fill="#888">手 ${xMax}</text>
  `;
  const baseline = baseY != null
    ? `<line x1="${P}" y1="${baseY}" x2="${W - P}" y2="${baseY}" stroke="#444" stroke-dasharray="2 3"/>`
    : "";

  return `
    <svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      ${baseline}
      ${fill}
      <path d="${d}" fill="none" stroke="${lineColor}" stroke-width="1.8" stroke-linejoin="round"/>
      ${yLabels}
      ${xLabels}
    </svg>
  `;
}

function renderMultiLine(series, opts = {}) {
  const W = 400, H = 140, P = 22;
  const allX = series.flatMap(s => s.points.map(p => p.x));
  if (allX.length === 0) return "";
  const xMin = Math.min(...allX), xMax = Math.max(...allX);
  const yMin = opts.yMin ?? 0;
  const yMax = opts.yMax ?? 100;
  const xSpan = Math.max(1, xMax - xMin);
  const xToPx = x => P + ((x - xMin) / xSpan) * (W - 2 * P);
  const yToPx = y => H - P - ((y - yMin) / (yMax - yMin)) * (H - 2 * P);

  const paths = series.map(s => {
    let d = "";
    let started = false;
    s.points.forEach(p => {
      if (p.y == null) { started = false; return; }
      d += (started ? "L" : "M") + xToPx(p.x) + "," + yToPx(p.y) + " ";
      started = true;
    });
    if (!d) return "";
    return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.6" stroke-linejoin="round"/>`;
  }).join("");

  return `
    <svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <line x1="${P}" y1="${yToPx(50)}" x2="${W - P}" y2="${yToPx(50)}" stroke="#333" stroke-dasharray="2 3"/>
      ${paths}
      <text x="4" y="${P + 4}"      font-size="9" fill="#888">${yMax}%</text>
      <text x="4" y="${H - P + 3}"  font-size="9" fill="#888">${yMin}%</text>
      <text x="${P}" y="${H - 4}"   font-size="9" fill="#888">手 ${xMin}</text>
      <text x="${W - P - 22}" y="${H - 4}" font-size="9" fill="#888">手 ${xMax}</text>
    </svg>
  `;
}

function playerStatsCard(p) {
  const card = document.createElement("div");
  card.className = "player-stats-card";
  if (p.personality === "human") card.classList.add("you");

  const persLabel = { human: "你", gto: "GTO", nit: "Nit" }[p.personality] || p.personality;

  // delta
  let deltaClass = "delta-zero";
  let deltaSign = "";
  if (p.chips_won > 0) { deltaClass = "delta-pos"; deltaSign = "+"; }
  if (p.chips_won < 0) { deltaClass = "delta-neg"; }

  const bb100 = p.bb_per_100 == null ? "—" : `${p.bb_per_100 > 0 ? "+" : ""}${p.bb_per_100}`;

  card.innerHTML = `
    <div class="psc-header">
      <div class="psc-name">${p.name}<span class="psc-pers">${persLabel}</span></div>
      <div class="psc-summary">
        ${p.hands} 手 ·
        <span class="${deltaClass}">${deltaSign}$${p.chips_won}</span> ·
        <span class="${deltaClass}">${bb100} BB/100</span>
      </div>
    </div>
    <div class="stats-grid">
      ${statCell("VPIP", p.vpip, p.raw.vpip_num, p.hands, healthyVPIP)}
      ${statCell("PFR",  p.pfr,  p.raw.pfr_num,  p.hands, healthyPFR)}
      ${statCell("3Bet", p.three_bet, p.raw.three_bet_num, p.raw.three_bet_opp, healthy3B)}
      ${statCell("F3B",  p.fold_to_three_bet, p.raw.folded_to_three_bet, p.raw.faced_three_bet, healthyF3B)}
      ${statCell("C-bet", p.cbet_flop, p.raw.cbet_flop_num, p.raw.cbet_flop_opp, healthyCBet)}
      ${statCell("WTSD", p.wtsd, p.raw.went_to_showdown, p.raw.saw_flop, healthyWTSD)}
    </div>
  `;
  return card;
}

function statCell(label, pct, num, den, healthFn) {
  let cls = "stat-cell";
  let valTxt = "—";
  if (pct == null) {
    cls += " dim";
  } else {
    valTxt = `${pct}%`;
    const verdict = healthFn(pct, den);
    if (verdict) cls += ` ${verdict}`;
  }
  return `
    <div class="${cls}">
      <div class="stat-label">${label}</div>
      <div class="stat-value">${valTxt}</div>
      <div class="stat-frac">${num}/${den}</div>
    </div>
  `;
}

// rough heuristics for 3-handed cash 100bb
function healthyVPIP(p, n) { if (n < 5) return ""; if (p < 25) return "low"; if (p > 55) return "high"; return "healthy"; }
function healthyPFR(p, n)  { if (n < 5) return ""; if (p < 18) return "low"; if (p > 45) return "high"; return "healthy"; }
function healthy3B(p, n)   { if (n < 5) return ""; if (p < 4)  return "low"; if (p > 14) return "high"; return "healthy"; }
function healthyF3B(p, n)  { if (n < 3) return ""; if (p < 35) return "low"; if (p > 75) return "high"; return "healthy"; }
function healthyCBet(p, n) { if (n < 3) return ""; if (p < 40) return "low"; if (p > 85) return "high"; return "healthy"; }
function healthyWTSD(p, n) { if (n < 5) return ""; if (p < 20) return "low"; if (p > 38) return "high"; return "healthy"; }

// ===== api =====
async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status}: ${t}`);
  }
  return r.json();
}

async function refresh() {
  state = await api("/api/state");
  render();
  driveAI();
}

async function newGame(opponents) {
  hideReviewPanel();
  state = await api("/api/new-game", {
    method: "POST",
    body: opponents ? { opponents } : null,
  });
  render();
  driveAI();
}

// ===== opponent picker =====
let _personalityCatalog = null;
let _pickedSeat1 = null;
let _pickedSeat2 = null;

async function openNewGamePicker() {
  const modal = $("#newgame-modal");
  modal.classList.remove("hidden");
  if (!_personalityCatalog) {
    try {
      _personalityCatalog = await api("/api/personalities");
    } catch (e) {
      alert("加载对手列表失败: " + e.message);
      modal.classList.add("hidden");
      return;
    }
  }
  // default to current selection
  const cur = _personalityCatalog.current || ["nit", "gto"];
  _pickedSeat1 = cur[0];
  _pickedSeat2 = cur[1];
  renderPicker();
}

function renderPicker() {
  const slot1 = $("#picker-seat1");
  const slot2 = $("#picker-seat2");
  slot1.innerHTML = "";
  slot2.innerHTML = "";
  _personalityCatalog.personalities.forEach(p => {
    slot1.appendChild(makeOption(p, 1));
    slot2.appendChild(makeOption(p, 2));
  });
}

function makeOption(p, slot) {
  const div = document.createElement("div");
  div.className = "picker-option";
  const isPicked = (slot === 1 ? _pickedSeat1 : _pickedSeat2) === p.key;
  if (isPicked) div.classList.add("selected");
  div.innerHTML = `
    <div class="po-name">${p.name} <span class="po-tag">${p.label}</span></div>
    <div class="po-desc">${p.desc}</div>
  `;
  div.addEventListener("click", () => {
    if (slot === 1) _pickedSeat1 = p.key;
    else _pickedSeat2 = p.key;
    renderPicker();
  });
  return div;
}

async function nextHand() {
  hideReviewPanel();
  state = await api("/api/new-hand", { method: "POST" });
  render();
  driveAI();
}

async function humanAction(action, amount = 0) {
  state = await api("/api/action", { method: "POST", body: { action, amount } });
  render();
  driveAI();
}

async function driveAI() {
  if (aiLoopBusy) return;
  if (!state || state.ai_to_act === null || state.ai_to_act === undefined) return;
  aiLoopBusy = true;
  try {
    while (state && state.ai_to_act !== null && state.ai_to_act !== undefined) {
      const seat = state.ai_to_act;
      const player = state.players.find(p => p.seat === seat);
      $("#thinking-name").textContent = `${player.name} 思考中…`;
      $("#thinking").classList.remove("hidden");
      try {
        state = await api("/api/ai-act", { method: "POST" });
      } catch (e) {
        console.error("AI error:", e);
        $("#thinking").classList.add("hidden");
        alert("AI 出错: " + e.message);
        break;
      }
      $("#thinking").classList.add("hidden");
      // bubble showing the action
      if (state.last_ai) {
        showBubble(state.last_ai.seat, state.last_ai);
      }
      render();
      // small delay so the player can read the bubble
      await new Promise(r => setTimeout(r, 600));
    }
  } finally {
    aiLoopBusy = false;
  }
}

function showBubble(seat, ai) {
  const seatEl = $(`#seat-${seat}`);
  if (!seatEl) return;
  const bubble = $(".action-bubble", seatEl);
  let txt = ACTION_CN[ai.action] || ai.action.toUpperCase();
  if (["bet", "raise", "call"].includes(ai.action)) {
    txt += ` $${ai.amount}`;
  }
  bubble.textContent = txt;
  bubble.classList.add("show");
  setTimeout(() => bubble.classList.remove("show"), 2200);
}

// ===== raise controls =====
function openRaiseControls() {
  const raiseBtn = $("[data-action='raise']");
  const min = parseInt(raiseBtn.dataset.minTo);
  const max = parseInt(raiseBtn.dataset.maxTo);
  const slider = $("#raise-slider");
  const input = $("#raise-input");
  slider.min = min; slider.max = max; slider.value = min;
  input.min = min; input.max = max; input.value = min;
  $(".raise-controls").classList.remove("hidden");
}
function closeRaiseControls() {
  $(".raise-controls").classList.add("hidden");
}

// ===== events =====
$$("#action-bar .action-buttons button").forEach(btn => {
  btn.addEventListener("click", async () => {
    const action = btn.dataset.action;
    if (action === "raise") {
      openRaiseControls();
      return;
    }
    if (action === "call") {
      const legal = state.your_legal_actions;
      const callAct = legal.actions.find(a => a.type === "call");
      await humanAction("call", callAct.amount);
    } else if (action === "fold") {
      await humanAction("fold");
    } else if (action === "check") {
      await humanAction("check");
    }
  });
});

$("#raise-slider").addEventListener("input", e => $("#raise-input").value = e.target.value);
$("#raise-input").addEventListener("input", e => {
  let v = parseInt(e.target.value || "0");
  const slider = $("#raise-slider");
  if (v < +slider.min) v = +slider.min;
  if (v > +slider.max) v = +slider.max;
  slider.value = v;
});

$$("#action-bar .quick-sizes button").forEach(btn => {
  btn.addEventListener("click", () => {
    const frac = btn.dataset.frac;
    const slider = $("#raise-slider");
    const max = +slider.max;
    const me = state.players.find(p => p.is_human);
    const toCall = state.current_bet - me.invested_this_street;
    const potIfCalled = state.pot + toCall;
    let target;
    if (frac === "all") {
      target = max;
    } else {
      // raise size in pot units → total to_amount = current_bet + size
      const size = Math.round(potIfCalled * parseFloat(frac)) + toCall;
      target = me.invested_this_street + size;
    }
    target = Math.max(+slider.min, Math.min(max, target));
    slider.value = target;
    $("#raise-input").value = target;
  });
});

$("#confirm-raise").addEventListener("click", async () => {
  const amt = parseInt($("#raise-input").value);
  closeRaiseControls();
  // raise vs bet — server accepts either as the same logic
  const raiseBtn = $("[data-action='raise']");
  const action = raiseBtn.firstChild.textContent.toLowerCase().trim();
  await humanAction(action, amt);
});
$("#cancel-raise").addEventListener("click", closeRaiseControls);

$("#btn-new-game").addEventListener("click", openNewGamePicker);
$("#btn-cancel-newgame").addEventListener("click", () => $("#newgame-modal").classList.add("hidden"));
$("#btn-confirm-newgame").addEventListener("click", () => {
  $("#newgame-modal").classList.add("hidden");
  newGame([_pickedSeat1, _pickedSeat2]);
});
$("#btn-next-hand").addEventListener("click", nextHand);
$("#btn-stats").addEventListener("click", openStats);
$("#btn-close-stats").addEventListener("click", () => $("#stats-modal").classList.add("hidden"));
$("#btn-history").addEventListener("click", openHistory);
$("#btn-close-history").addEventListener("click", () => $("#history-modal").classList.add("hidden"));
$("#session-select").addEventListener("change", () => {
  loadStatsForSelected().catch(e => console.error("session switch failed", e));
});

// review panel: 整个 header 都可点折叠
$("#review-header").addEventListener("click", e => {
  // 别让 toggle 按钮的点击冒泡过来又翻一次
  if (e.target.closest(".review-toggle")) return;
  toggleReviewPanel();
});
$("#review-toggle").addEventListener("click", toggleReviewPanel);

$("#btn-coach").addEventListener("click", async () => {
  const btn = $("#btn-coach");
  const note = $("#coach-note");
  btn.disabled = true;
  btn.textContent = "教练复盘中…";
  note.classList.remove("hidden");
  note.textContent = "教练复盘中…";
  try {
    const r = await api("/api/coach");
    note.textContent = r.note;
    btn.textContent = "已复盘";
  } catch (e) {
    note.textContent = "复盘失败: " + e.message;
    btn.disabled = false;
    btn.textContent = "请教练复盘";
  }
});

// keyboard shortcuts
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT") return;
  if (state?.street === "hand_over") return;
  if (e.key === "f") $("[data-action='fold']").click();
  if (e.key === "c") {
    const checkBtn = $("[data-action='check']");
    if (!checkBtn.classList.contains("hidden") && !checkBtn.disabled) checkBtn.click();
    else $("[data-action='call']").click();
  }
  if (e.key === "r") $("[data-action='raise']").click();
});

// boot
refresh();
