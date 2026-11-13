<div align="center">

# 🐋 Self-Iteration Loop

**一个跑在本地的自迭代循环：从候选池选题 → 花真钱执行 → 机器分验收 → 能涨才留档。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3772FF?logo=python&logoColor=white)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-v4-4B8BBE)](https://github.com/AstrBotDevs/AstrBot)

[English](#-english) · [中文](#-中文)

</div>

---

## 📖 English

### What this is

A complete self-iteration loop that runs on your own machine:

```
candidate pools → pick → execute → machine-scored review → archive → settle the bill → back to pools
```

`core/` is the loop itself — pure standard library, no AstrBot, no model weights.
`main.py` is a thin AstrBot plugin that only **renders** the loop's state in chat.

### The three hard constraints

Most "AI self-improvement" stops at the prompt layer: it says it got better, and nobody checks.
Here three things are made non-negotiable:

1. **Only three things can change** — memory, tools/skills, and the scheduling code. Model
   weights are frozen; "I tuned my own parameters" is rejected outright.
2. **Every change needs an archive entry and a rollback path.** No archive, no adoption.
3. **Every round is billed in real money.** The wallet hits zero and the loop stops.

### The five pools

| Pool | Purpose |
|---|---|
| `curiosity` | External signals (trending feeds etc.) turned into open questions, with TTL and de-dup |
| `ape_pool` | Mutation candidates for one goal — several variants, keep only what actually scores higher |
| `practice_pool` | Cheap low-stakes drills before touching anything real |
| `inspiration` | Raw ideas that don't fit a goal yet |
| `goals` | The real queue: each goal carries a `level` and an explicit pass condition |

### Review is mechanical, not self-reported

`score_judge.py` + `score_rubric.json`. Anything computable is computed — the model is not asked
to grade its own homework. A rubric item that two runs of the same work can't reproduce is a bug.

### The bill

`ledger.py` marks the start and the end of a round; the difference is what the round actually cost.
`gate()` turns the balance into a state: normal → experiments paused → everything paused.
Replies are never gated, only autonomous spending is.

### Layout

```
core/      the loop: iter_cycle, curiosity, ape_pool, practice_pool,
           inspiration, ledger, score_judge, score_report, score_panel,
           housekeep, collect_signals, perf_audit + score_rubric.json
main.py    AstrBot plugin (read-only view; /selfdrive commit is admin-only)
docs/      SELF_EVOLUTION.md (the method), PRIORITY.md (value ordering)
examples/  goals.example.json
```

### Quick start

1. Put `core/` anywhere on your machine.
2. In the AstrBot plugin config, set `self_dir` to that path.
3. `python core/iter_cycle.py --help` to see the round switches.
4. Runtime state (`wallet.json`, `archive.jsonl`, `log.md`, `curiosity.md`, …) is generated
   locally and **git-ignored on purpose** — those files contain your own history, not code.

### What is not included

No model weights, no API keys, no conversation logs, no personal archive.
This is the machinery, not the operator's diary.

---

## 中文

### 这是什么

一套跑在你自己机器上的自迭代循环，一整条链路：

```
候选池 → 选题 → 执行 → 机器评分验收 → 留档 → 结账 → 回池子
```

`core/` 是循环本体，纯标准库，不依赖 AstrBot、不依赖任何模型权重；
`main.py` 是壳，只把循环状态搬进聊天窗口，默认只读。

### 三条硬约束

大部分所谓"AI 自我进化"停在 prompt 层：说改好了，没人验。这里把三件事做成硬的：

1. **只能改三样东西**——记忆、工具/技能、调度流程。模型权重是冻的，"我调了自己的参数"这种话直接否掉。
2. **每次改动必须有归档和回滚路径**。没归档就不许采纳，涨了才算数。
3. **每轮真金结账**。钱包见底就停，不许自己给自己续命。

### 五个池子

| 池 | 干什么 |
|---|---|
| `curiosity` | 把外部信号（热搜等）变成待探索的问题，带 TTL 和去重，过期就扔 |
| `ape_pool` | 同一目标的变异候选：一次生成多个版本，只留评分真的涨上去的那个 |
| `practice_pool` | 低成本练手题，先在小事上试错，再动真东西 |
| `inspiration` | 暂时挂不上目标的零散灵感 |
| `goals` | 真正的队列：每条带 `level` 和明确的过线条件 |

### 验收不靠自我感觉

`score_judge.py` + `score_rubric.json`。能机械算的绝不给模型算——自己改、自己执行、自己打分，
是这个循环最容易烂掉的地方，所以评分项写成外部可复现的。同一件活跑两次出不来同一个分，那条 rubric 就是坏的。

### 账单

`ledger.py` 在整轮两端打标记，差值就是这一轮真烧掉的钱；`gate()` 把余额变成闸门状态：
正常 → 停实验 → 全停。**回话永不受影响，只有自主花费被闸。**

### 目录

```
core/      循环本体（12 个脚本 + 评分标准）
main.py    AstrBot 插件壳（只读视图，commit 仅管理员）
docs/      方法论 SELF_EVOLUTION.md、价值序 PRIORITY.md
examples/  goals 结构示例
```

### 上手

1. 把 `core/` 放到任意目录；
2. 插件配置里把 `self_dir` 指过去；
3. `python core/iter_cycle.py --help` 看轮次开关；
4. `wallet.json` / `archive.jsonl` / `log.md` / `curiosity.md` 这些是**运行时自己生成的**，
   故意写进 `.gitignore`——那是操作者的日记，不是代码。

### 不含什么

不含模型权重、不含任何 API key、不含对话日志与个人档案。
这里给的是机器，不是操作员的日记本。
