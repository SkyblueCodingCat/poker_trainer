# 德州扑克训练场 · Poker Trainer

> *English summary at the bottom.*

一个**和 LLM 对手玩 3 人桌德州扑克**的本地训练工具。每个对手都是一次独立、无状态的 LLM
调用，互相看不到底牌或思考过程——所以信息隔离不是约定，而是架构层面的物理保证。

为练习 NLHE（无限德州扑克）的决策、读对手画像、复盘自己的漏洞而设计。**不是**赌博工具，
没有真钱、没有联网对战，所有牌局只在你本地浏览器里。

---

## 为什么做这个

学德州扑克最大的瓶颈之一是**找不到合适的练习对手**：

- 真钱桌：又贵又慢，错误成本高，反馈周期以周计
- 训练软件：内置 bot 通常只有一种风格，往往是经典 GTO 或简单规则机
- 找朋友打：很难凑齐稳定时间，朋友之间还得讲点情面

LLM 让一种新可能成立——**你可以同时面对 4 种性格鲜明的对手，他们用自然语言"思考"，
打完每手还能用人话告诉你为什么这么打**。这是练 hand reading 和针对性策略的理想场景。

这个项目把这个想法做成了能跑的工具：3 人桌、4 种对手风格、PT4 风格统计、SVG 趋势图、
教练复盘——一切都在你机器上跑，对手就是后端的几次 API 调用。

---

## 功能特性

### 核心玩法

- 🎲 **完整 NLHE 引擎**：盲注、最小加注、按位轮转、all-in、边池、摊牌、5 张最佳手评估。纯 Python 实现，零外部赌博库依赖。
- 🎰 **真实牌桌 UI**：绿桌椭圆、扑克牌花色字、盲注 button、轮到谁动金色高亮、AI 思考时屏幕中央显示"…思考中"
- ⌨️ **键盘 + 鼠标**：`F` 弃牌、`C` 跟注/过牌、`R` 加注；加注带 ½ / ⅔ / 1× / 2× pot / 全下 一键尺寸

### 4 种对手风格

| 角色 | 风格 | 特征 | 训练价值 |
|---|---|---|---|
| 🪨 **Stone** | Nit 极紧 | VPIP ≈ 15%，下重注必有牌 | 练弃牌纪律、识别强 range |
| 🦈 **Shark** | GTO 平衡 | 50%+ VPIP，混合 bluff/value | 标准答案参照 |
| 🐺 **Wolf** | LAG 松凶 | 65% open，12% 3-bet，多 barrel | 练 bluff catch、抗压力 |
| 🐠 **Goldfish** | Calling Station | 几乎不弃牌、几乎不加注 | 练 thin value、耐心 |

每种风格都是一个完整的中英混合 system prompt（在 `backend/personalities.py`），描述了
preflop range、postflop 倾向、sizing 偏好、典型台词。重开一局可以**任选两个**当对手。

### 数据面板

每打完一手，后端从 action history 自动推断每个玩家的：

- **VPIP** / **PFR** / **3Bet%** / **Fold-to-3Bet%** / **C-bet%** / **WTSD%** —— PT4 标准 6 大核心 stats
- **手数** / **净盈亏** / **BB/100** —— 总体水平指标
- 颜色编码：绿色=健康范围、黄色=偏高、红色=偏低、灰色=样本量不够

### 趋势图

纯 SVG（无 chart.js / d3 等依赖）：

- **盈亏曲线**：累计 chips_won 随手数变化，正赢绿填充、负输红色
- **风格趋势**：VPIP 蓝线 + PFR 金线叠加，看你打着打着是越来越紧还是越来越松

### AI 教练复盘

- 每手结束自动弹复盘窗口（先显示摊牌底牌+赢家），点 **「请教练复盘」** 才会调用教练 AI
- 后端**先用引擎算好每个摊牌玩家的最终成牌等级 + 5 张牌**作为 ground truth 写进 prompt，避免 LLM 把两对说成"A 高"
- 输出格式固定：【关键决策】【分析】【判定】【建议】，200 字以内
- 用中文叙述，专业术语保留英文（c-bet / equity / range / pot odds / polarized / TPTK 等）

---

## 工作原理：信息隔离怎么保证

```
                 玩家 (You) 在浏览器里看到自己的底牌 + 公共牌 + 历史动作
                                  │
                  ┌───────────────┼───────────────┐
                  │                                │
            HTTP /api/state                   HTTP /api/action
                  │                                │
            ┌─────▼────────────────────────────────▼─────┐
            │    FastAPI 后端 / GameState (单一真相)        │
            │   - 牌堆、底池、街道、轮转 in memory          │
            │   - 知道每个玩家的底牌                       │
            └────┬─────────────────────────────────────┬──┘
                 │                                     │
        当 to_act 是 AI 对手时 ↓               推完进入下一手 ↓
                 │
        ┌────────▼──────────────┐
        │   ai_player.decide()  │
        │  - 只取该对手的视角     │ ← 关键：只装入这个对手能看到的信息
        │  - 套上他的 personality │
        │  - 一次性 LLM 调用      │
        └────────┬──────────────┘
                 │
       ┌─────────▼─────────┐         ┌─────────▼─────────┐
       │ Stone (Nit) 调用   │  vs vs  │ Shark (GTO) 调用   │   ← 完全独立
       │ system: nit prompt│         │ system: gto prompt│      两次 API
       │ user: Stone 视角  │         │ user: Shark 视角  │      没有共享
       │       底牌+公共牌+ │         │       底牌+公共牌+ │      上下文
       │       历史动作    │         │       历史动作    │
       └───────────────────┘         └───────────────────┘
```

**为什么 Stone 看不到 Shark 的底牌？** 因为 Stone 那次调用的 prompt 里**根本就没有那段文本**。
后端在构造 prompt 时只把 `state.players[stone_seat].hole_cards` 加进去，Shark 的 hole_cards
留在内存里没被序列化进 LLM 输入。

不像让 AI"假装看不见"——是物理上看不见。

---

## 技术栈

- **后端**：Python 3.10+ / FastAPI / uvicorn
- **前端**：原生 HTML + CSS + Vanilla JS（零构建步骤、零 npm 依赖、加载即跑）
- **AI**：Anthropic Claude（默认，推荐 Opus 系列）或 OpenAI GPT，由 `LLM_PROVIDER` 切换
- **统计**：内置纯 Python 推断模块，PT4 风格的分子/分母累加
- **图表**：纯 SVG 手写，无图表库

整个项目大约 1500 行代码（含 prompt 文本），单人一晚能读完。

---

## 截图（占位）

> *截图待补。运行后浏览器进 `http://localhost:8765/` 体验。建议先打 5 手感受 Stone 和 Goldfish
> 的风格差异，再切到 Shark + Wolf 体验 GTO vs LAG 的压力对抗。*

---

## 快速开始

```bash
# 1. 克隆并进入项目
git clone git@github.com:SkyblueCodingCat/poker_trainer.git
cd poker_trainer

# 2. 准备 Python 环境
cd backend
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. 配置 LLM provider
cp ../.env.example ../.env
# 编辑 .env，至少填一个 provider 的 API key
# 或者直接 export 到 shell：
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-你的key

# 4. 启动服务
uvicorn main:app --reload --port 8765
```

打开浏览器访问 `http://localhost:8765/`，开打。

### 用 OpenAI

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-你的key
export OPENAI_MODEL=gpt-4o          # 或 gpt-4o-mini / o1-mini / 任何 chat 模型
uvicorn main:app --reload --port 8765
```

### 用 Anthropic 兼容的中转 / 代理

公司或服务商的中转地址（不是官方 api.anthropic.com）：

```bash
export LLM_PROVIDER=anthropic
export ANTHROPIC_BASE_URL=https://your-relay.example.com/anthropic
export ANTHROPIC_AUTH_TOKEN=你的中转token
export POKER_AI_MODEL=claude-opus-4-5    # 中转上的特定 model id
```

### 用本地模型（LM Studio / Ollama 等）

通过 LM Studio 开 OpenAI 兼容 server 后：

```bash
export LLM_PROVIDER=openai
export OPENAI_BASE_URL=http://localhost:1234/v1
export OPENAI_API_KEY=lm-studio        # 占位，本地不校验
export OPENAI_MODEL=qwen2.5-7b-instruct # 你装好的模型名
```

⚠️ 本地小模型决策质量会明显下降，prompt 设计假设的是 Opus / GPT-4 级别的模型。

---

## 怎么玩

- 你坐底部，筹码 $200，盲注 $1/$2，**3 人桌**：你 + 2 个 AI 对手
- 顶部 **「重开一局」**：4 选 2 任意搭配对手风格
- 顶部 **「📊 数据」**：看累计 stats + 趋势曲线
- 行动按钮：Fold / Check / Call / Raise（带快捷尺寸）
- 键盘：`F` 弃、`C` 跟/过、`R` 加
- 每手结束：自动弹复盘窗口；点 **「请教练复盘」** 才调用教练 AI（避免每手耗 token）

### 推荐训练剧本

| 想练的能力 | 选这两个对手 | 关键决策点 |
|---|---|---|
| 弃牌纪律 | Stone + Wolf | Wolf 大 3-bet 时分清是 bluff 还是 value |
| Thin value | Goldfish + Shark | Goldfish 看到 pair 都跟，多下 1/3 pot 收割 |
| Range 平衡 | Shark + Wolf | 两个会施压的对手，逼你不能只玩 nuts |
| 抗大池压力 | Wolf + Wolf 风格混合 | （想加第二个 LAG 可在 personalities.py 里复制） |

---

## 性能 / 成本

每手大概 2-5 次 LLM 调用（取决于走到哪条街），加上摊牌时如果你点教练复盘 +1 次。

按 Claude Opus 4 定价（$15/M 输入 / $75/M 输出）粗估：
- 一次决策调用约 1500 token 输入 + 100 token 输出 ≈ **$0.03**
- 一次教练复盘约 2000 token 输入 + 400 token 输出 ≈ **$0.06**
- **平均一手约 $0.10-0.15**，玩 100 手 $10-15 美元

用 Sonnet 或 Haiku 能再降 70%-90%，但对手 prompt 设计得依赖较强模型才能拿到好的中英混合风格输出，
你可以根据预算自己调整。

---

## 架构

```
poker_trainer/
├── backend/
│   ├── game.py             # 纯德扑引擎（牌堆、回合、摊牌、5 张比大小）
│   ├── personalities.py    # 4 种对手 system prompt
│   ├── ai_player.py        # 把 GameState 打包成对手视角，通过 llm.chat() 发出去
│   ├── llm.py              # Anthropic / OpenAI provider 抽象层
│   ├── stats.py            # VPIP/PFR/3Bet/... 推断 + 累加 + 时间线快照
│   ├── main.py             # FastAPI 路由 + 前端静态托管
│   └── requirements.txt
├── frontend/
│   ├── index.html          # 牌桌 UI + 弹窗
│   ├── style.css           # 全部样式
│   └── app.js              # 交互逻辑、SVG 折线图绘制
├── .env.example
├── LICENSE                 # MIT
└── README.md
```

每个文件都有顶部 docstring 说明职责。

---

## 自定义对手

在 `backend/personalities.py` 的 `PERSONALITIES` 字典里加一个新条目：

```python
TIGHT_FISH = """
You are "Carp", a tight-passive fish at a 3-handed cash table.
... 你的风格描述（preflop range / postflop 倾向 / 典型台词） ...
"""

PERSONALITIES = {"nit": NIT, "gto": GTO, "lag": LAG, "station": STATION,
                 "carp": TIGHT_FISH}
PERSONALITY_LABELS = {..., "carp": {"name": "Carp",
                                    "label": "Tight Fish",
                                    "desc": "紧弱型，玩太宽但又不敢主动"}}
```

重启服务，新对手会自动出现在选择器里。

写好 prompt 的关键：**preflop range 要写具体到组合、postflop 要给出"看到 X 时通常做 Y"的规则、加几句典型台词**——别光说"this player is aggressive"。

---

## 已知限制

- **样本量小时 stats 不准**：3-Bet% / F3B% 等需要几十手才有统计意义
- **教练偶尔风格漂移**：极短的牌局（preflop 就结束）会被识别为"无关键决策"，输出占位提示
- **all-in + 多边池**：实现了边池但少量边缘场景未深度测试，发现 bug 请开 Issue
- **不持久化**：刷新或重启服务会清空牌历和 stats（设计如此——这是训练场不是数据库）
- **3 人桌固定**：架构上引擎支持 2-9 人，但 UI 只画了 3 个座位
- **AI 偶尔违反 personality**：LLM 不是约束系统，约 5% 决策会"出 character"

---

## Roadmap

如果有时间继续做，下一步可能加：

- [ ] 6-max 桌（UI 重画 6 个座位）
- [ ] Hand history 持久化（SQLite）+ 跨 session 累积 stats
- [ ] 对手 stat 实时显示在 HUD 里（PT4 / HM3 风格）
- [ ] 多语言 UI（先做英文版）
- [ ] equity 即时计算（Monte Carlo），辅助复盘
- [ ] 锦标赛模式（变化盲注、淘汰、ICM）
- [ ] 自定义 prompt 编辑器（不用改代码就能加对手）

如果你做了任何扩展或者发现 bug，欢迎开 Issue / PR。

---

## License

MIT — see [LICENSE](LICENSE). 自己练习、教学、改造、二次开发都没问题，注明出处即可。

---

## English Summary

3-handed No-Limit Hold'em training tool where each opponent is an independent
stateless LLM call (Claude or GPT). They literally cannot see each other's
hole cards or reasoning — information isolation is a property of the
architecture, not a rule we ask them to follow.

**Highlights**:
- Pure-Python NLHE engine (blinds, min-raise, all-in, side pots, showdown)
- 4 distinct opponent archetypes via system prompts: Nit / GTO / LAG / Calling Station
- Realistic poker table UI (vanilla HTML/CSS/JS, no build step, no npm)
- PT4-style stats panel (VPIP, PFR, 3Bet, Fold-to-3Bet, C-bet, WTSD, BB/100)
- Pure SVG win/loss curve and VPIP/PFR trend chart
- AI coach review at end of each hand (200 words, with engine-computed final
  hand category as ground truth so the coach can't misread your cards)
- Provider abstraction for Anthropic + OpenAI (incl. Anthropic-compatible
  relays and OpenAI-compatible local servers like LM Studio)

**Setup**: Python 3.10+, FastAPI, an Anthropic OR OpenAI API key. See "快速开始"
above. UI text and AI dialogue are bilingual: Chinese narration with English
poker jargon (c-bet, equity, pot odds, polarized, etc.).

**For training/educational use only. Not a real-money gambling tool.**
