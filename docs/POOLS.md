# 八个池子

每个池子只干一件事，产出的候选是 `{id, kind, title, why, cost_rmb, ...}`。

| 池 | 料源 | 干什么 |
|---|---|---|
| curiosity 好奇 | `curiosity.py` + `topic_store.py` | 抓全网高频词 → 分档（前沿科技 > 现有技术 > 热点 > 政经 > 梗）→ 过滤掉煽动/饭圈 → 入池 |
| practice 练习 | `practice_pool.py` | 补"理论与实操"的缝：拆成最小可验证动作 |
| inspiration 灵感 | `inspiration.py` | 维度碰撞：把不相关的两件事撞出可做的小东西 |
| optimize 优化 | `optimize.py` | 加法（补最小功能单元，功能正确分高）与减法（去重、减延迟、删无关功能，需备份） |
| introspect 自省 | `introspect.py` | 从自己留下的记录里推"接下来该干什么"，不靠外部喂 |
| maintain 维护 | `maintain_audit.py` | 机械判据巡检：信号链、账本同步、端口、备份 |
| archive 归档 | `archive_pool.py` | 把散落的过程产物整理成可查的档案 |
| learn 学习 | `learn.py` + `curriculum.py` | 按课程表找"没学过但该学"的技能，课程表是私货位 |

附加料源：`info_pool.py`（多源 RSS/GitHub/arXiv 全量收，先存不判）、
`ape_pool.py`（个人片单池，数据走 `local/ape_videos.json`）、`source_probe.py`（信息源探针）。

## 权重口径

优化池的加减法不是对半：减法**排除核心功能**并乘 0.7，加法权重必须大于减法，
否则越"优化"越空。核心功能（引擎、开关、记账，以及被直接 import 的模块）整体豁免。
