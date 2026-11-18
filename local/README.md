# local/ —— 私货位

公开版故意不带这些文件：它们装的是**你的**余额、你的机器、你的课表、你的口味。
代码里对每个文件都是"读得到就用，读不到就降级"，所以删空也能跑。

放一份到 `$SELFDRIVE_HOME/`（不设就是 `core/` 旁边）。

| 文件 | 谁在用 | 说明 |
|---|---|---|
| `wallet.json` | `iter_cycle` | 本地钱包：余额、每日配额、花销流水 |
| `balance.json` | `iter_cycle` | 最近一次余额快照（`{balance, ts, src}`） |
| `bio_signals.json` | `iter_cycle` | 宿主侧信号：负载、端口、作息。**只有降级默认值也能跑** |
| `health.json` | `iter_cycle` | 自身健康：连续失败、上次成功时间 |
| `curriculum.json` | `curriculum.py` → `learn` | 课程表，格式见本目录 `curriculum.json.example` |
| `ape_videos.json` | `ape_pool` | 片单数据（`[{bvid, title, duration, pubdate}]`） |
| `goals.json` / `goals_*.json` | `iter_cycle` / 各池 | 目标条目，池子会往里写候选 |
| `switches.json` | `switchboard` | 链路/功能开关，缺了按全开 |
| `hotwords.txt` / `veto.txt` | `curiosity` | 关键词白名单 / 屏蔽词，一行一条 |
| `jev/ledger.jsonl` | `jev_ledger` | 打分台账，一行一条：`{ts, pool, kind, title, score, by}` |

每个 `.example` 都是能直接改名用的最小样例（记得把值换成你自己的）。
