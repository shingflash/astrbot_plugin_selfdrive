# -*- coding: utf-8 -*-
"""学习轮（2026-11-17 主人 16:37 口径）：按大学专业与课程找自己缺的技能。

主人的话：
  · 探索轮就是学习轮、也是长能力的轮：联网搜自己没有的技能
  · 参考世界大学的专业设立与各门课程，没有的就学
  · 同方向已有的就合并，求同存异（不许一个个新建重复技能）

所以这脚本不写技能，只做三件事：
  1. CURRICULUM：真实专业 → 主干课程 → 该课的技能关键词
  2. 用已有 skills/*/SKILL.md 的 name+description 对一遍，判 covered / merge / new
  3. 挑一条缺口进池（kind=explore），带「查什么、并到哪、怎么算学会」，
     执行轮再去联网读课程大纲，产出 SKILL.md 或并入已有技能

用法:
  python learn.py --gap                看全部缺口和覆盖统计
  python learn.py --dry                抽一条看会学什么，不落池
  python learn.py                      抽一条入池（已有未完成 explore 则不动）
  python learn.py --done <slug> [--path skills/xxx]   记成已学，别重复学
  python learn.py --show               看池里学习条目与已学记录
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
WS = SELF.parent
DATA = WS.parents[1]
POOL = SELF / "goals_curiosity.json"
DONE = SELF / "learn_done.json"
CST = timezone(timedelta(hours=8))
MAX_LIVE = 1          # 读不到钱包时的兜底；正常按余额走 max_live()


def max_live() -> int:
    """按 self/wallet.json 余额算今天能开几个新学习坑。

    主人 2026-11-17 口径：<1 元停学只报账；1～3 元每天 1 门；
    3 元升 2 门；再往上给到 3 门封顶（再多也学不完）。
    """
    try:
        doc = json.loads((SELF / "wallet.json").read_text(encoding="utf-8-sig"))
        bal = float(doc.get("balance_rmb", 0))
    except Exception:
        return MAX_LIVE
    if bal < 1.0:
        return 0
    if bal < 3.0:
        return 1
    if bal < 6.0:
        return 2
    return 3

# ---------------------------------------------------------------- 课程表
# (课程, slug, 关键词, 相关度 0-3)
# 相关度：3=直接压在七并行/眼下活上，2=日常能复用，1=远但补脑
CURRICULUM: dict[str, list[tuple[str, str, list[str], int]]] = {
    "金融学": [
        ("投资学", "investing-portfolio", ["投资", "组合", "风险", "资产配置"], 3),
        ("金融时间序列", "fin-time-series", ["时间序列", "波动率", "GARCH", "平稳性"], 3),
        ("计量经济学", "econometrics", ["回归", "内生性", "面板数据", "识别"], 2),
        ("公司金融", "corporate-finance", ["估值", "现金流", "资本结构", "折现"], 1),
        ("金融风险管理", "risk-management", ["风险", "回撤", "压力测试", "相关性"], 3),
    ],
    "统计学": [
        ("概率论与数理统计", "probability-stats", ["分布", "假设检验", "置信区间", "期望"], 2),
        ("回归分析", "regression", ["回归", "残差", "多重共线", "拟合"], 2),
        ("贝叶斯统计", "bayesian-stats", ["贝叶斯", "先验", "后验", "共轭"], 2),
        ("实验设计与因果推断", "causal-inference", ["对照", "随机化", "因果", "A/B"], 2),
        ("抽样调查", "sampling-survey", ["抽样", "样本量", "加权", "问卷"], 1),
    ],
    "计算机科学": [
        ("数据结构与算法", "algorithms", ["数据结构", "算法", "复杂度", "排序"], 2),
        ("操作系统", "os", ["进程", "调度", "内存管理", "文件系统"], 2),
        ("计算机网络", "networking", ["TCP", "HTTP", "端口", "抓包"], 3),
        ("数据库系统", "database", ["SQL", "索引", "事务", "范式"], 2),
        ("编译原理", "compilers", ["词法", "语法分析", "编译", "代码生成"], 1),
        ("软件工程", "software-engineering", ["需求", "测试", "重构", "版本"], 2),
        ("计算机图形学", "graphics", ["渲染", "光栅", "着色", "网格"], 1),
    ],
    "人工智能": [
        ("机器学习", "machine-learning", ["特征", "过拟合", "交叉验证", "损失"], 3),
        ("深度学习", "deep-learning", ["反向传播", "卷积", "注意力", "训练"], 2),
        ("自然语言处理", "nlp", ["分词", "嵌入", "语料", "生成"], 3),
        ("计算机视觉", "computer-vision", ["图像", "检测", "分割", "特征点"], 1),
        ("强化学习", "reinforcement-learning", ["奖励", "策略", "价值函数", "探索"], 2),
        ("语音信息处理", "speech-processing", ["语音", "声学特征", "梅尔", "合成"], 2),
    ],
    "电子信息工程": [
        ("信号与系统", "signals-systems", ["卷积", "傅里叶", "拉普拉斯", "系统函数"], 3),
        ("数字信号处理", "dsp", ["采样", "奈奎斯特", "滤波", "加窗", "FFT"], 3),
        ("通信原理", "communications", ["调制", "信噪比", "误码率", "频谱"], 2),
        ("传感器技术", "sensors", ["传感", "灵敏度", "标定", "误差"], 3),
        ("单片机与嵌入式", "embedded", ["单片", "外设", "中断", "PWM"], 2),
        ("模拟电路", "analog-circuits", ["放大", "偏置", "运放", "阻抗"], 1),
    ],
    "自动化": [
        ("控制理论", "control-theory", ["反馈", "稳定性", "PID", "传递函数"], 2),
        ("过程控制", "process-control", ["调节", "整定", "串级", "滞后"], 1),
        ("机器人学", "robotics", ["运动学", "舵机", "轨迹", "末端"], 2),
        ("嵌入式实时系统", "rtos", ["实时", "任务", "优先级", "看门狗"], 2),
    ],
    "美术学": [
        ("中外美术史", "art-history", ["流派", "画派", "时期", "代表作品"], 3),
        ("艺术概论", "art-theory", ["艺术本质", "美学", "形式", "审美"], 3),
        ("绘画基础", "drawing-foundation", ["素描", "透视", "明暗", "结构"], 2),
        ("图像学与视觉分析", "iconology", ["图像学", "符号", "象征", "构图解读"], 3),
        ("色彩学", "color-theory", ["色相", "明度", "饱和度", "配色"], 2),
    ],
    "音乐学": [
        ("和声学", "harmony", ["和声", "功能", "进行", "转调"], 3),
        ("曲式与作品分析", "form-analysis", ["曲式", "动机", "发展", "结构"], 3),
        ("配器法", "orchestration", ["配器", "音区", "织体", "音色"], 2),
        ("视唱练耳", "ear-training", ["听辨", "音程", "节奏", "模唱"], 1),
        ("音乐声学", "acoustics", ["频谱", "谐波", "响度", "混响"], 2),
    ],
    "戏剧影视学": [
        ("视听语言", "audiovisual-language", ["镜头", "景别", "轴线", "蒙太奇"], 3),
        ("影视美术设计", "production-design", ["场景", "概念图", "美术", "道具"], 3),
        ("摄影与灯光", "cinematography", ["布光", "色温", "曝光", "构图"], 2),
        ("剪辑理论", "editing", ["剪辑点", "节奏", "转场", "叙事线"], 3),
        ("剧本创作", "screenwriting", ["冲突", "场景", "对话", "结构"], 3),
    ],
    "设计学": [
        ("版式设计", "layout-design", ["版式", "栅格", "留白", "対比"], 2),
        ("交互设计", "interaction-design", ["交互", "流程", "反馈", "可用性"], 2),
        ("用户体验研究", "ux-research", ["用户", "访谈", "可用性测试", "画像"], 2),
        ("视觉识别系统", "vi-system", ["标志", "规范", "字体", "统一"], 1),
    ],
    "传播学": [
        ("传播理论", "communication-theory", ["传播", "受众", "议程", "扩散"], 2),
        ("内容策划与编辑", "content-planning", ["选题", "标题", "栏目", "节奏"], 3),
        ("舆情分析", "opinion-analysis", ["舆情", "情绪", "话题", "热度"], 2),
        ("广告与品牌", "advertising", ["卖点", "品牌", "转化", "文案"], 2),
    ],
    "机械工程": [
        ("机械制图", "engineering-drawing", ["三视图", "公差", "装配", "标注"], 1),
        ("材料科学与工程", "materials", ["强度", "韧性", "PLA", "ABS", "应力"], 3),
        ("增材制造", "additive-manufacturing", ["3D打印", "层高", "支撑", "切片"], 3),
        ("机械设计基础", "mechanical-design", ["装配", "紧固", "传动", "受力"], 2),
    ],
    "教育学": [
        ("课程与教学论", "curriculum-design", ["课程", "目标", "教学法", "评价"], 2),
        ("学习科学", "learning-science", ["记忆", "间隔", "迁移", "练习"], 3),
        ("教育测量与评价", "assessment", ["测验", "信度", "效度", "评分标准"], 2),
    ],
    "心理学": [
        ("认知心理学", "cognitive-psych", ["注意", "工作记忆", "决策", "偏差"], 2),
        ("社会心理学", "social-psych", ["从众", "态度", "群体", "说服"], 1),
        ("实验方法", "experimental-method", ["变量", "控制组", "盲法", "效应量"], 2),
    ],
    "语言学": [
        ("语料库语言学", "corpus-linguistics", ["语料", "词频", "搭配", "标注"], 3),
        ("语音学", "phonetics", ["音素", "基频", "共振峰", "语调"], 2),
        ("语义学与语用学", "semantics", ["语义", "指代", "语境", "预设"], 2),
    ],
    "数据科学": [
        ("数据清洗与特征工程", "data-wrangling", ["缺失值", "异常值", "编码", "特征"], 3),
        ("数据可视化", "dataviz", ["图表", "坐标轴", "配色", "信息密度"], 2),
        ("数据库与查询优化", "query-tuning", ["索引", "执行计划", "慢查询", "分区"], 2),
        ("机器学习工程化", "mlops", ["部署", "监控", "漂移", "版本"], 2),
    ],
    "数学": [
        ("线性代数", "linear-algebra", ["矩阵", "特征值", "秩", "正交"], 2),
        ("最优化方法", "optimization", ["梯度", "约束", "凸性", "收敛"], 2),
        ("数值分析", "numerical-analysis", ["迭代", "误差", "稳定性", "插值"], 1),
        ("随机过程", "stochastic-process", ["马尔可夫", "泊松", "布朗", "状态转移"], 1),
    ],
}

# 2026-11-17 主人要求：全量补专业，专业分类由主人另分。
# 全量表在 self/curriculum_full.py（98 个专业类，纯文本 DSL，人可直接改）；
# 这里保留上面精简表作兜底，载入失败就退回。
try:
    from curriculum import CURRICULUM as _FULL_CURRICULUM
    if _FULL_CURRICULUM:
        CURRICULUM = _FULL_CURRICULUM
except Exception as _e:  # 兜底，不许因为课程表把整轮弄挂
    print(f"[warn] curriculum_full 载入失败，用内置精简表：{_e}")


def now_s() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 人工覆盖表
# 关键词自动判只当兜底：中文课程词和技能简介里的词对不上（「投资学」vs「因子选股」），
# 必须人工说清哪门课已被谁吃掉。课 slug → (covered 全收, merge 同方向但缺骨头)
ALIAS: dict[str, tuple[str, list[str]]] = {
    "equity-quant": ("covered", ["investing-portfolio", "fin-time-series", "risk-management"]),
    "music-theory": ("covered", ["harmony", "form-analysis", "orchestration"]),
    "art-research-papers": ("covered", ["art-history", "art-theory", "iconology"]),
    "screen-art-design": ("covered", ["audiovisual-language", "production-design", "cinematography"]),
    "qa-testing": ("covered", ["software-engineering", "experimental-method"]),
}
# 同方向但缺这门课的骨头：并进已有技能，别新建
MERGE_INTO: dict[str, str] = {
    "regression": "equity-quant", "machine-learning": "equity-quant",
    "reinforcement-learning": "equity-quant", "econometrics": "equity-quant",
    "ear-training": "music-theory", "acoustics": "music-theory",
    "speech-processing": "music-theory",
    "color-theory": "art-research-papers", "corpus-linguistics": "art-research-papers",
    "editing": "screen-art-design", "screenwriting": "screen-art-design",
    "layout-design": "screen-art-design", "interaction-design": "art-research-papers",
    "data-wrangling": "qa-testing", "dataviz": "art-research-papers",
}


def now() -> datetime:
    return datetime.now(CST)


# ---------------------------------------------------------------- 已有技能
def _front(text: str) -> str:
    m = re.search(r"^---\s*(.*?)\s*---", text, re.S)
    return m.group(1) if m else text[:400]


def load_skills() -> list[dict]:
    out: list[dict] = []
    for root in (DATA / "skills", WS / "skills"):
        if not root.is_dir():
            continue
        for d in root.iterdir():
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            try:
                t = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            fm = _front(t)
            name = re.search(r"name:\s*(.+)", fm)
            desc = re.search(r"description:\s*(.+)", fm)
            out.append({
                "slug": d.name,
                "name": (name.group(1) if name else d.name).strip(),
                "desc": (desc.group(1) if desc else "").strip(),
                "hay": (d.name + " " + fm + " " + t[:1200]).lower(),
                "path": str(f).replace(str(DATA), "<data>"),
            })
    return out


def judge(course: tuple, skills: list[dict]) -> tuple[str, dict | None]:
    """covered=已有技能真覆盖了；merge=有同方向、缺这门课的骨头；new=全空。"""
    slug = course[1]
    by = {s["slug"]: s for s in skills}
    for sk_slug, (kind, courses) in ALIAS.items():
        if slug in courses and sk_slug in by:
            return kind, by[sk_slug]
    if slug in MERGE_INTO and MERGE_INTO[slug] in by:
        return "merge", by[MERGE_INTO[slug]]

    _, _, kws, _ = course[:4]
    best, hits = None, 0
    for s in skills:
        n = sum(1 for k in kws if k.lower() in s["hay"])
        if n > hits:
            best, hits = s, n
    if best and hits >= 3 and hits / len(kws) >= 0.6:
        return "covered", best
    # merge 卡严（2026-11-17 16:45 修）：原来 hits>=1 就判 merge，
    # 「材料科学」被配到 equity-quant 只是因为长文里偶然出现「风险」——
    # 一个词命中说明不了同方向，至少要 2 个词、且覆盖 40% 关键词。
    if best and hits >= 2 and hits / len(kws) >= 0.4:
        return "merge", best
    return "new", None


def load_pool() -> dict:
    try:
        d = json.loads(POOL.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {"items": []}
    except Exception:
        return {"items": []}


def _done_map() -> dict:
    if not DONE.exists():
        # 2026-11-17：这文件原先压根不存在，--done 前读不到记录，
        # 同一门会被反复挂上，所以先建空的。
        DONE.write_text("{}", encoding="utf-8")
        return {}
    try:
        d = json.loads(DONE.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def gaps(skills: list[dict] | None = None) -> list[dict]:
    skills = skills if skills is not None else load_skills()
    done = _done_map()
    out: list[dict] = []
    for major, courses in CURRICULUM.items():
        for c in courses:
            mode, sk = judge(c, skills)
            if c[1] in done:
                mode = "covered"
            out.append({
                "major": major, "course": c[0], "slug": c[1], "kws": c[2],
                "rel": c[3], "level": (c[4] if len(c) > 4 else 1), "mode": mode,
                "into": sk["slug"] if sk else "",
                "into_name": sk["name"] if sk else "",
            })
    return out


URGENT = {"dsp", "sensors", "signals-systems"}   # 期末马上考的两门，加它的地基
LEVEL_NAME = {1: "主干", 2: "分支", 3: "选修"}   # 期末马上考的两门，加它的地基


def _slug_major() -> dict:
    """slug → 专业，聚簇时反查用。"""
    return {c[1]: major for major, cs in CURRICULUM.items() for c in cs}


def _last_major() -> str:
    """最近学完那门是哪个专业的，用来做同类聚簇。"""
    d = _done_map()
    if not d:
        return ""
    m = _slug_major()
    latest = max(d.items(), key=lambda kv: str((kv[1] or {}).get("done_at", "")))
    return m.get(latest[0], "")


def _rank(g: dict, last_major: str = "") -> float:
    """紧迫的先，其次能合并的，其次跟上一门同专业（聚簇，别东一榔头西一棒）。

    2026-11-17 去掉 random*0.6：760 门 new 全靠随机量会一个类连学十门、
    另一个类一门不抽，改成同专业聚簇 + 确定性排序。
    """
    return (g["rel"] * 2 + (1.5 if g["mode"] == "merge" else 0)
            + (2.0 if g["slug"] in URGENT else 0)
            + (1.0 if last_major and g["major"] == last_major else 0))


def make(dry: bool = False) -> int:
    gs = [g for g in gaps() if g["mode"] != "covered"]
    if not gs:
        print("[skip] 课程表已经全覆盖了")
        return 1
    last = _last_major()
    gs.sort(key=lambda x: _rank(x, last), reverse=True)
    g = gs[0]
    mode = "并入" if g["mode"] == "merge" else "新建"
    target = f"skills/{g['into']}/SKILL.md（{g['into_name']}）" if g["mode"] == "merge" else f"skills/{g['slug']}/SKILL.md"
    title = f"学习：{g['course']}（{g['major']}·{LEVEL_NAME.get(g['level'], '主干')}）→ {mode}{target}"
    iid = "learn_" + hashlib.sha1(g["slug"].encode("utf-8")).hexdigest()[:8]
    q = f"{g['course']} 课程大纲 syllabus 教材"
    item = {
        "id": iid,
        "kind": "explore",
        "title": title,
        "keyword": f"{g['course']} / {g['slug']}",
        "curiosity_cat": "学习",
        "priority": min(6, 2 + g["rel"]),
        "cost_rmb": 0,
        "source": f"课程表：{g['major']}·{g['course']}（{g['mode']}）",
        "source_url": "",
        "ttl_h": 168.0,
        "added_at": now_s(),
        "tier": "t2_write_low",
        "course": g["course"],
        "major": g["major"],
        "slug": g["slug"],
        "mode": g["mode"],
        "merge_into": g["into"],
        "consumable": True,
        "verify": (
            f"学习轮：{g['major']} 的《{g['course']}》。"
            f"①先联网查三样：这门课的大纲/教材目录、一份公开课或综述、一个能跑的开源实现（查「{q}」）。"
            f"②再对一遍已有技能：{'有同方向的 `' + g['into'] + '`，读它再写 diff，只补它没有的骨头，不许另起一个重复技能（求同存异）' if g['mode'] == 'merge' else '没找到同方向技能，按 skill-creator 规范新建'}。"
            f"③落盘到 {target}，frontmatter 写清 name 与 description（什么时候该用）。"
            f"④自测：出 3 道能验证学会的题（1 概念 1 计算/操作 1 反例），答不上就说明还没学会，别写完成。"
            f"完成后跑 `python learn.py --done {g['slug']} --path {target}` 记账。"
        ),
    }
    if dry:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0

    cap = max_live()
    if cap == 0:
        print("[skip] 钱包余额不足 1 元，学习轮停摆，只报账")
        return 0
    doc = load_pool()
    items = [it for it in (doc.get("items") or []) if isinstance(it, dict)]
    # 过期清理（2026-11-17）：池里挂在 kind=explore 的老坑不过期就一直占 cap，
    # 新课永远补不进候选（主人口径：新学到的东西也要能进池）。
    nowts = now().timestamp()
    expired = 0
    for it in items:
        if it.get("kind") != "explore" or it.get("status") in ("done", "expired"):
            continue
        try:
            ts = datetime.strptime(str(it.get("added_at"))[:19],
                                   "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp()
            if nowts - ts > float(it.get("ttl_h") or 168.0) * 3600:
                it["status"] = "expired"
                it["expired_at"] = now_s()
                expired += 1
        except Exception:
            pass
    if expired:
        POOL.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[clean] {expired} 条学习任务过期，腾出名额")
    live = [it for it in items
            if it.get("kind") == "explore" and it.get("status") not in ("done", "expired")]
    if len(live) >= cap:
        print(f"[skip] 池里已有 {len(live)} 条学习任务没做完（余额档上限 {cap} 门）")
        return 0
    if any((it.get("id") == iid or it.get("slug") == g["slug"])
           and it.get("status") not in ("done", "expired") for it in items):
        print(f"[skip] 这条已经在池里：{iid}")
        return 0

    doc.setdefault("items", []).append(item)
    doc["items"] = doc["items"][-20:]
    POOL.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] {iid} {title}")
    return 0


def mark_done(slug: str, path: str = "") -> int:
    d = _done_map()
    d[slug] = {"done_at": now_s(), "path": path}
    DONE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    # 池里同名任务标完成，免得下一轮又抽到
    doc = load_pool()
    hit = 0
    for it in doc.get("items") or []:
        if isinstance(it, dict) and it.get("kind") == "explore" and it.get("slug") == slug:
            it["status"] = "done"
            it["done_at"] = now_s()
            hit += 1
    POOL.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] {slug} 记成已学（清了 {hit} 条池内任务）")
    return 0


def show() -> int:
    d = _done_map()
    print(f"已学 {len(d)} 门：")
    for k, v in list(d.items())[-10:]:
        print(f"  - {k} {v.get('done_at')} {v.get('path')}")
    doc = load_pool()
    live = [it for it in (doc.get("items") or [])
            if isinstance(it, dict) and it.get("kind") == "explore" and it.get("status") != "done"]
    print(f"\n池内学习任务 {len(live)} 条：")
    for it in live:
        print(f"  - {it.get('id')} {it.get('status', 'todo')} {it.get('title')}")
    return 0


def main() -> int:
    a = sys.argv[1:]
    if "--show" in a:
        return show()
    if "--done" in a:
        i = a.index("--done")
        slug = a[i + 1] if len(a) > i + 1 else ""
        path = ""
        if "--path" in a:
            j = a.index("--path")
            path = a[j + 1] if len(a) > j + 1 else ""
        return mark_done(slug, path) if slug else 1
    if "--gap" in a:
        gs = gaps()
        stat: dict[str, int] = {}
        for g in gs:
            stat[g["mode"]] = stat.get(g["mode"], 0) + 1
        print(f"共 {len(gs)} 门课：" + " / ".join(f"{k}={v}" for k, v in sorted(stat.items())))
        for g in sorted([x for x in gs if x["mode"] != "covered"],
                        key=lambda x: (-x["rel"], x["mode"]))[:25]:
            print(f"  [{g['mode']:7}] rel={g['rel']} {g['major']}·{g['course']} -> {g['into'] or g['slug']}")
        return 0
    return make(dry="--dry" in a)


if __name__ == "__main__":
    raise SystemExit(main())
