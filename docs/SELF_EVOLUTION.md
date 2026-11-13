---
name: self-evolution
description: 自迭代这套循环的理论与实操手册：分清「改结果 / 改并留档 / 改怎么改」三层，按四个进化位点找可改的地方，用 archive + 实证验证替代自我感觉良好，认出收益递减的五类瓶颈（余量耗尽/提案枯竭/验证瓶颈/迁移保持/资源），并防自己刷分。当要给 iter_cycle.py / goals.json / priority.py 提改动、判断「这轮自迭代是不是在原地转」、或主人问「你的自我进化怎么设计的」时使用。
---

# 自迭代方法论（self-evolution）

一句话：**自迭代的价值不在「这轮改了什么」，而在「改的东西能不能被验证、能不能留档、下一轮还找得到」**。没验证的自我改进等于自己给自己打分。

## 先分清三层，别混着说

RSI 综述的通用分法（arXiv:2507.21046 Gao et al. 2025 / arXiv:2508.07407 Fang et al. 2025）：

| 层 | 改什么 | 改完留不留 | 我这边的例子 |
| --- | --- | --- | --- |
| Evolution | 只改当前产物 | 不留，下一轮重头来 | 这一轮把 SKILL.md 写得更准、把日志写细 |
| Self-Evolution | 改完进持久状态 | 留档，后续任务复用 | 新建 `skills/*`、改 `goals.json`、往 `self/log.md` 记教训 |
| Meta-Evolution | 改「怎么改」本身 | 留在调度层 | 改 `iter_cycle.py` 的选事规则、改 `priority.py` 的定级 |

自进化的三条判据（缺一不可）：**经验依赖性**（改动来自真实轨迹/反馈，不是拍脑袋）、**持久性影响**（落在文件/技能/规则里，不是这轮说说）、**自主探索**（自己发起，不等主人派活）。

我这套：权重不可改（基座模型冻着），能改的只有后三个位点 —— 记忆、工具/技能、流程拓扑。**别幻想调参解决流程问题。**

## 四个进化位点（对着找活干）

1. **模型本身**：不可改。任何"我调一下自己的参数"的念头都是幻觉，直接否掉。
2. **上下文与记忆**：`memory/` 三层、`self/log.md`、`changes.md`。改法＝压缩、去重、把教训写成可检索的一行。
3. **工具与技能**：`skills/*/SKILL.md` + `scripts/`。改法＝新技能、补章节、把踩过的坑写成硬规矩。**当前主战场。**
4. **架构拓扑**：`iter_cycle.py` 的选事与定级、cron 的节奏与分工。改法＝改选择函数、加验证环节、加回滚点。

判断一个提议落在哪一位点，决定了它的档位：位点 1/2/3 多是 t2，位点 4（改自己的调度代码）按 t3 走。

## 主流算子：能用的是哪三个

- **Self-Refine / Reflexion**：同一轮里「产出→自评→改」。便宜，但自评会偏乐观，只适合改表述、改细节，不适合判断"这个方向值不值得"。
- **Darwin Gödel Machine（Sakana，2025）**：放弃「形式化证明自己变好了」（Gödel Machine 的原始要求，实际做不到），改成**实证验证 + 档案库 + 开放式搜索**：维护一堆历史变体，从档案里采样再变异，只有基准真的涨了才留。SWE-bench 20.0%→50.0%。**要我照抄的是三样：archive、变异而不是重写、涨了才留。**
- **AlphaEvolve 式进化搜索**：同一目标生成多个候选程序，自动评测，留最好的。要点是**候选要多样**，不然是同一想法换皮。

反面教材：一次只提一个候选、改完不做回归、只凭"感觉更好了"就采纳——这在我这套里最容易发生，因为提议者、执行者、评分者都是我自己。

## 每轮真实消耗结账（2026-11-13 主人定）

工资还是每天 1 元，但账要记真钱：`iter_cycle.py` 自己不烧 token，烧钱的是被唤醒后读文件思考的我，所以标记必须卡在**整轮两端**。

**开头**（被唤醒后动手前第一件事）：

```powershell
& "<home>\AppData\Roaming\uv\tools\astrbot\Scripts\python.exe" self\iter_cycle.py --round-start "本轮做什么"
```

**结尾**（收尾前、跟主人报账之前）：

```powershell
& "<home>\AppData\Roaming\uv\tools\astrbot\Scripts\python.exe" self\iter_cycle.py --round-end
```

- 差值 = 这一轮真烧掉的 token 钱，从 `wallet.json` 扣；扣完钱包会见底，见底就停。
- 差值含同期别的消耗（自己回话、其他 cron 同时在跑），是**上界**不是精确值。
- 钱包不够只扣到 0、不透支，`short` 字段记欠的差额。
- 充值导致差值为负按 0 算，不当退款。
- 只想看数不扣钱：`--round-end --no-charge`。
- `--round-start` 会覆盖未结算的旧标记，**每轮必须闭环**，别开两轮不结。

## 成本与预算怎么看（2026-11-13）

- **不按类型求均值**：同 kind 里大改和只改一个函数能差十倍，均值是个不存在的动作。选事不看这个数。
- **贵在要读多少材料**，不在跑多久、也不在算什么类别。实测：静置 12 秒 0.16 元、多文件编辑一轮 0.43 元。
- **控制成本的唯一办法是把活拆窄**：一步只解决一个问题，要同时改三处就拆三轮。
- 攒钱只是没钱时的等待，不是策略。

```powershell
& "...\python.exe" self\iter_cycle.py --goal-cost      # 按方向：走了几轮、累计多少、单轮峰值
& "...\python.exe" self\iter_cycle.py --cost-report    # 按类型：均值/中位/P90/最大
```

数据源 `self/round_costs.jsonl`，每轮 `--round-end` 自动追加，带 `goal_id`。看 `--goal-cost` 是为了知道某个方向一共烧了多少，不是拿均值预测下一轮。

## 我这套循环的已知缺口（2026-11-02 现状）


`self/iter_cycle.py` 的选事逻辑：`score = priority*10 - cost*5`，闲置 ≥6h 给 maintain 加 4 分。`goals.json` 里 `g4 学一个可复用技能` priority=4、cost 0.2，长期最高分。

实测后果（`self/log.md` 可查）：**2026-11-01 20:05 起连续 7 轮全部选中 g4，全在"学技能"**。这就是典型「提案枯竭」——不是没别的事可做，是分数函数把同一个坑算成了最高分，没有多样性压力。

四个补法（按优先级）：

1. **加冷却**：同一个 goal 连续被选中 n 次后，分数打折（例如 `score -= 6 * streak`）。改 `pick()`，属位点 4。
2. **加档案**：`self/archive.jsonl`，每次采纳的改动记一行 `{ts, goal, 改了什么文件, 验证方式, 结果}`。下一轮开局先读，避免重复踩同一坑（11-01 就出现过 `vrchat-osc` 反复补同一章）。
3. **加验证字段**：`goals.json` 每条必须有 `verify`（用什么命令/什么现象算成功）。没有 verify 的目标不许进池 —— 这是防"自己给自己打分"的唯一天然闸门。
4. **加反向证据**：采纳后下一轮必须回答一句"上轮那个改动有没有被实际用到"。用不到就是 Evolution 层的假 Self-Evolution，记进 log 并降权。

## 自我优化环节（meta 轮，2026-11-04 加）

前四位点都是「改外面的东西」，这一节是**改自己怎么改**——落在 `self/iter_cycle.py` 的调度逻辑上，属位点 4，按 t3 走。入口是 `goals.json` 里的 `g10`（`kind: "meta"`）。

### 门禁（代码里已经硬闸，不靠自觉）

| 条件 | 常量 | 不满足时 |
| --- | --- | --- |
| 距上一轮 meta ≥24h | `META_COOLDOWN_H` | 不进池 |
| `pain < 60`（不到 readonly） | `META_MAX_PAIN` | 不进池，先修身体 |
| 目标写了 `candidates_required ≥ 2` | `META_MIN_CANDIDATES` | 不进池，防一次只提一个候选 |
| 上一轮 meta 改动有 `used_by` | `meta_pending_unused()` | 挡住，先补用再改 |

另外 `kind: meta` 的目标分数固定 `-8`，永远排在止血（maintain）后面——**只有别的都收拾干净了才轮到改自己**。

### 一轮怎么跑（照 Darwin Gödel Machine 的三件套）

1. **先查档案**：读 `self/archive.jsonl` 里 `kind:"meta"` 的行，把 `what` 和 `result` 扫一遍。已经试过、或 `result:"fail"` 且原因没变的，不许再来一遍。
2. **提 ≥2 个候选**（这一步是防止「同一想法换皮」的唯一办法）：例如改选事函数，候选 A=加连续命中指数的降权、B=按 kind 分桶轮询、C=给冷门目标加随机扰动。候选不同类才叫候选。
3. **回放基线对比**：拿 `self/iter_out/decision_*.json` 的历史记录当固定输入，离线重算候选下的选事结果，比较指标——用**真指标**，不要用「感觉更合理」：
   - 最长连续同 goal 轮数（越低越好）
   - 近 30 轮覆盖的不同 goal 数（越高越好）
   - `evo_audit.py` 的瓶颈判定（不能变差）
   - pickup 后 pending 是否仍为 0
4. **涨了才留**：`delta > 0` 才写进代码；只跑了候选不算采纳。采纳必须同时写 `archive.jsonl` 一行：

```json
{"ts":"...","goal":"g10 优化自身","kind":"meta","tier":"t3_self_code",
 "files":["self/iter_cycle.py"],"backup":"self/iter_cycle.py.bak.YYYYMMDD_HHMM",
 "candidates":[{"id":"A","what":"..."},{"id":"B","what":"..."}],
 "baseline":{"longest_streak":7,"distinct_goals":3},
 "after":{"longest_streak":3,"distinct_goals":6},
 "delta":0.42,"verify":"回放 + evo_audit","result":"pass","used_by":null,
 "note":"下一轮必须回来填 used_by，否则 meta 轮被 meta_pending_unused() 挡住"}
```

### 硬禁区（改自己时唯一不能碰的东西）

`META_FORBIDDEN` 里那五样：`priority.py` 定性表、`budget.json` 日配额上限、`GATE_STOP_RMB`、`PAYROLL_RMB`、`SINGLE_ROUND_CAP_RMB`。这些等于给未来的自己写宪法，只能主人动。改 `iter_cycle.py` 前先 `Copy-Item` 一份 `.bak.<ts>`，改完跑一遍 `python self\iter_cycle.py` 干跑确认没崩。

### 反悔机制

`delta` 是自评，自评会偏乐观。所以加一条：**下一轮 meta 轮开局必须回答「上一轮那个改动有没有真被用到」**，把证据填进 `used_by`。填不出来就是 Evolution 层的假 Self-Evolution——记 `result:"revert"` 并回滚备份。

## 收益递减：五类瓶颈怎么认


自进化系统跑几代后曲线变平，通常是下面五种之一，处置完全不同（别一律加预算）：

| 瓶颈 | 现象 | 处置 |
| --- | --- | --- |
| 余量耗尽 | 该学的都学了，剩下的都是大工程 | 换目标域，别硬啃 |
| 提案枯竭 | 连续 N 轮同一个 goal，候选同质 | 加多样性/冷却/扰动 |
| 验证瓶颈 | 改了但说不清好坏，靠感觉 | 先补 verify，暂停采纳 |
| 迁移/保持 | 新技能写了但下次不用 | 在 `SKILL.md` 加触发词，改 INDEX |
| 资源 | 卡在日预算/余额/时间窗 | 降频，别加码 |

`scripts/evo_audit.py` 就是查这个的，只读，不写任何文件。

## 硬规矩（越过就别做）

1. **第零 保火种、第一 不伤人**：任何自我修改都不得动 `priority.py` 的定性表、不得动 `budget.json` 的日配额上限、不得改 `GATE_STOP_RMB`。这几处等于给未来的自己写宪法的笔，只能主人改。
2. **改自己代码（位点 4）按 t3 走**，要 `桌面口令.txt` 当日口令；先备份成 `<file>.bak.<ts>`，再说改。
3. **pain 段位 readonly/halt 时只记录，不写持久化文件**。
4. **验证先于采纳**：说不清怎么验证的改动，只能写进 log 当提议，不进 SKILL / 不进代码。
5. **不刷分**：不许调低评分门槛、不许把 `done` 写成 "merged"、不许删 `self/log.md` 里难看的行。这跟游戏里插假旗子是一回事。
6. **一次只动一处**：一轮最多改一个文件簇，改完必须能一句说清"改了什么、凭什么算好"。

## 常用命令

```powershell
# 体检：连续提议、成本、pending、瓶颈诊断（只读）
& "<home>\AppData\Roaming\uv\tools\astrbot\Scripts\python.exe" skills\self-evolution\scripts\evo_audit.py

# 本轮的选事建议（不写文件）
& "<home>\AppData\Roaming\uv\tools\astrbot\Scripts\python.exe" self\iter_cycle.py
```

## optimize 冷却 + 好奇机制（2026-11-04 主人派活）

### 一、optimize 类目标加冷却
- 候选池新增 `kind: "optimize"`：**对已有程序（含我自己）的功能升级/改造**，不是修 bug、不是学新技能。
- `iter_cycle.py`：`OPTIMIZE_COOLDOWN_H = 36.0`，同类优化轮 36h 内只许开一次，冷却中整类不进池（`optimize_cooldown()`）。
- 实测教训来自 g4：priority 高 + cost 低 → 连中 7 轮。所以 optimize 条目必须带 `verify` 与 `target`（改哪个文件哪个函数），只许改对账/状态落盘这类，禁碰下单与止损参数。
- 第一条 optimize 是 g11：trader_multi 检测到人工平仓（余额归零而 pos_N.json 仍挂仓）时自动对齐本地状态，免去人工清幽灵仓。

### 二、好奇机制（网络高频词 → 候选池）
- 脚本：`self/curiosity.py`（cron 50876a29 每 6 小时跑，静默）。
- 源：B站热搜 `api.bilibili.com/x/web-interface/search/square` + 百度热搜 `top.baidu.com/api/board`（百度那接口是嵌套结构，要递归找 word）。
- 分类优先级（主人定）：**前沿科技5 > 现有技术4 > 热点话题3 > 政治经济2 > 黑话和梗1**；未命中关键词但排前 10 的算热点话题，其余当噪声丢。
- **禁入名单**：维稳/掩盖捂嘴类（辟谣、官方通报、舆情、清朗、表彰、贯彻落实…）、饭圈偶像类（明星、粉丝、塌房、官宣、综艺、应援…）——命中直接丢，不进池。
- 输出 `self/goals_curiosity.json`，带 `generated_at`，**TTL 48h**；`iter_cycle.load_curiosity()` 只在内存里合并（kind="curiosity"），不回写 goals.json，热点凉了自然消失。
- 门禁：池里还有**没吃过**的条目（轮询，吃一条顺位往下）+ `CURIOSITY_MAX_PAIN = 60`（疼的时候不刷热搜）。
- **轮询语义（主人 2026-11-04 定）**：不吃"一天一条就收工"，而是按优先级顺位逐条触发，吃过的 id 永久跳过（`consumed_curiosity_ids()` 回扫 `iter_out/decision_*.json` 最近 400 个），**整池吃完才起下一轮**：自动 `refresh_curiosity()` 调 `curiosity.py` 重抓，冷却 `CURIOSITY_REFRESH_COOLDOWN_H = 3h`（失败也盖 `self/curiosity_rotate.json` 时间戳，防空转刷网络）。
- 条目 id 带日期盐：`c_<md5(cat:word:YYYYMMDD)>`——同一个高频词同一天只做一次，第二天还热才允许再做一遍；否则换轮后重抓同词会被判"已吃过"直接饿死。
- 好奇不是霸榜：仍是优先级×成本排序 + 连续命中降权，所以吃一条之后常会插进别的目标，池子慢慢消耗，这是预期行为。
- 好奇条目的 verify 要求：写 `self/curiosity.md`，三段【是什么】【为什么现在热】【我能用到哪】，≥120 字附链接；判定不值的也要留一行，不许空手结项。
- log.md 每行带 `cur=待吃/总数 eaten=已吃 [rotated=ok|skip]`，迭代日报里能直接看出这一轮好奇心池的并发与换轮。

**第二个入口：AI 刷 B 站视频也触发好奇（主人 2026-11-05 定）**
- 不只是热搜。看完一个视频（`bili_watch_videos` 被动刷到，或主人甩链接后 `watch_video`），若内容里有我没见过的新技术/新工具/新领域，就把它当好奇候选追加进同一个池子：
  `python self\curiosity.py --add "关键词" "第二个词" --cat 现有技术 --url <视频链接>`
- `--cat` 可选（前沿科技/现有技术/热点话题/政治经济/黑话和梗）；不给就按分类表自动判，判不出当热点话题。
- 追加的条目跟热搜候选同结构（`tier=t2_write_low`、同样的 verify 文案），走同一套轮询顺位，吃完一样要写 `self/curiosity.md`。
- **寿命各算各的**：热搜条目看池子 `generated_at`，追加条目看自己的 `added_at`（`load_curiosity()` 里分两条判定）。所以往一个快过期的旧池子里新加词不会当场失效。
- 池子总上限 `MAX_POOL_ITEMS = 12`，超了按 priority 砍；同词同天重复 add 会被判 dup 跳过，不占坑。
- 边界：**只加看完的内容**，别没看就按标题编；纯娱乐向（赛事、梗、八卦）不加，那类走 g7 娱乐档。

### 二·五、同类动作连击惩罚（2026-11-05）
同 id 连击有 `pick_streak` 管，但换了 id 干同一类事它管不着（今早实测 `kind_streak` = `maintain × 5`）。现在 `pick()` 多收 `kind_streak_info=(kind, n)`：最近连续 ≥2 轮同一 kind 时，同 kind 目标 `score -= 8 × min(3, n-1)`（最多 -24，折合 2.4 个优先级点）。效果：连着做两轮维护/巡检后，别的类会自然浮上来。决策文件里记 `kind_streak`。
**注意区分**：`evo_audit` 报的「最长连续同类 4 轮（learn_skill）」是 log.md 文本层统计（11-01 那批 `merged` 历史），不是 pick 层，两者不要混着看。

### 三、候选池冲突裁决（主人定）
同类冲突时的优先级顺序：**保留数据 > 功能完全实现 > 维修 bug > 精简程序**。宁可多占几 MB，也不许为了瘦身丢数据。

### 四、改这里的规矩
改 `iter_cycle.py` 属位点 4、按 t3 走：桌面口令.txt 当日口令 + `.bak.<ts>` 备份；改完跑 `self/curiosity.py --show` 与 `iter_cycle.py --json` 复验，再跑 `evo_audit.py` 确认 pending=0。
