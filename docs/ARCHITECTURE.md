# 架构

## 一圈

`core/iter_cycle.py` 是主循环，本身不调模型、不下判断，只产出"这一轮该干什么"：

1. **外部信号**：`health.json`（自身健康）、`bio_signals.json`（宿主/机器信号，私货位）、
   端口探针、台账（`jev/ledger.jsonl`）。
2. **痛值**：`compute_pain()` 把信号合成 0-10 的痛值，决定这一轮是 normal / throttle /
   readonly / halt。段位只约束"一轮最多烧多少"，日配额不在这里。
3. **挑活**：各池给出候选（带 kind、预估成本、理由），
   `value_bonus(kind)` = 台账实测分经收缩加权 → 加分，再叠加：新鲜度衰减（`fatigue`）、
   同 id 24h 重复惩罚（`convergence`）、方向偏离惩罚（`direction`）、开关（`switchboard`）。
4. **落盘**：`--commit` 时写 `decision.json` + 追加 `log.md`，并把候选记进 `jev/rounds.jsonl`。

## 闸门

| 层 | 内容 |
|---|---|
| 段位 | normal 5 元/轮、throttle 2 元/轮、readonly 与 halt 为 0 |
| 余额 | 每轮开工前查余额，不足按 `enough_for(days)` 拦 |
| 疲劳 | 同一 kind 连续出现按次数衰减，避免刷同一类活 |
| 收敛 | 同 id 24h 内重复出现要扣分，并在 decision 里注明 |
| 开关 | 链路总开关 + 功能子开关，`switchboard.on(path)`；模块缺失默认**开** |

## 判官

`core/score_judge.py`：0-10 分，先机械判据（能跑？产物在？指标过？），再按 rubric 加权。
人工分权重 3 倍，自评权重 1 倍，同一条记录人工覆盖自评（`jev_ledger.append` 的 by 字段）。

## 记账与回灌

`core/jev_ledger.py`：每池算加权均分 → 下一轮挑活时的加分：

```
bonus = (avg * w + 4 * 12) / (w + 12)
```

`w` 是加权样本量（人工 3、自评 1）。样本少自动往中位分 4 靠：
这是 2026-11-18 改的，之前是"样本够 3 就直接用实测均分"，结果某个池靠 4 条 9.5 分的
样本把加分顶到 9.5，越干越偏同一件事。

## 为什么没有"自我修改"

代码里没有任何自动改自己源码的路径。优化池产生的候选只是"建议 + 机械判据 + 两种改法"，
由人（或外面的 agent）决定改不改、改完再回归。守则：能自己改的只有记忆和工具。
