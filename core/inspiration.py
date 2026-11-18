# -*- coding: utf-8 -*-
"""灵感发生器（2026-11-17 改版：维度碰撞版）。

主人 16:32 的新口径：
  · 灵感要「推陈出新」——拿新鲜热点词去碰已有的技术/思路（AI 漫剧碰音乐、碰美术、碰影视）
  · 「碰一个的实现概率越低，就多碰一个」：二元组太容易落地就别硬凹，概率低的再加一维
  · 题目和好奇同级，可供给其他轮次挑，但不是必须来源

所以撞料从「两条旧料随机拼一句」改成「热点 × 领域1（× 领域2）」：
  1. 热点来自 self/hotwords.txt（人工/检索后写）或 self/info/inbox_*.jsonl 的外部标题
  2. 领域来自 DOMAINS 关键词表，每个领域标注跟哪个技能库有积累
  3. 先算二元组实现概率 p2；p2 > 0.38 就二元收工，否则再跨族补一维成三元
  4. 三条维度跨族（创作 / 工程 / 交易）才是真碰撞；同族撞出来的是近亲

规则见 self/linkage.md 第六节。产出只能是「如果 A 像 B，那么 C」，C 必须能测。

用法:
  python inspiration.py --dry        只看会撞出什么（含概率与维数），不落池
  python inspiration.py              撞 1 条进池（池里已有 MAX_LIVE 条未验证灵感则不动）
  python inspiration.py --show       看台账和池里的灵感条
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
DATA = WS.parents[1]                 # <data>
POOL = SELF / "goals_curiosity.json"
LEDGER = SELF / "inspiration.md"
HOTFILE = SELF / "hotwords.txt"
CST = timezone(timedelta(hours=8))
MAX_LIVE = 2          # 池里同时挂 2 条就够，别挤掉好奇
TTL_H = 168.0         # 灵感不追热点，一周
SKIP = re.compile(r"^(```|>|#|\||\s*$)")

# ---------------------------------------------------------------- 领域表
# 顺序即抽取优先级：靠前的领域更容易被抽到（跟我眼下在干的事对齐）
DOMAINS = {
    "音乐": ["音乐", "作曲", "编曲", "和声", "混音", "旋律", "乐理", "伴奏", "BGM", "音源", "节奏"],
    "美术": ["美术", "绘画", "构图", "配色", "插画", "笔触", "视觉", "写实", "色相", "素材"],
    "影视": ["电影", "镜头", "分镜", "运镜", "剪辑", "剧集", "叙事", "灯光", "漫剧", "短剧"],
    "动漫": ["番剧", "动画", "角色", "原作", "声优", "漫改", "播出", "二次元", "漫画"],
    "模型": ["模型", "校准", "概率", "推理", "训练", "embedding", "分类头", "token", "上下文", "提示词"],
    "工程": ["脚本", "函数", "进程", "端口", "配置", "报错", "重构", "部署", "依赖", "缓存"],
    "测试": ["测试", "回归", "用例", "复现", "验证", "断言", "覆盖率", "QA"],
    "数据": ["数据", "统计", "日志", "报表", "指标", "可视化", "SQL", "台账", "抽样"],
    "硬件": ["显卡", "显存", "CPU", "散热", "内存", "功耗", "驱动", "外设", "装机"],
    "游戏": ["游戏", "关卡", "平衡", "玩家", "手感", "数值", "mod", "抽卡"],
    "量化": ["量化", "因子", "回测", "仓位", "止损", "套利", "波动", "持仓", "交易"],
    "文本": ["文案", "标题", "写作", "排版", "校对", "结构", "稿"],
    "语音": ["语音", "TTS", "配音", "朗读", "音色", "念白", "语音合成"],
    "自动化": ["cron", "定时", "轮询", "流水线", "触发", "报警", "调度"],
    "教学": ["教程", "讲解", "笔记", "学习", "课程", "科普"],
}

# 族：跨族的两个领域才算「碰」，同族是近亲繁殖（撞出来像同义反复）
FAMILY = {
    "音乐": "创作", "美术": "创作", "影视": "创作", "动漫": "创作", "文本": "创作", "教学": "创作",
    "模型": "工程", "工程": "工程", "测试": "工程", "数据": "工程", "硬件": "工程",
    "游戏": "工程", "自动化": "工程", "语音": "工程",
    "量化": "交易",
}

# 领域 → 现成技能库（有积累 = 落地概率高）。key 是 skills 目录名里的关键词
SKILL_DOMAINS = {
    "art": "美术", "music": "音乐", "screen-art": "影视", "equity": "量化", "quant": "量化",
    "qa-testing": "测试", "documents": "文本", "spreadsheets": "数据", "skill-creator": "工程",
    "saturation": "模型", "bridge": "自动化",
}


def now() -> datetime:
    return datetime.now(CST)


def now_s() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def living_skills() -> set[str]:
    """本机真有积累的领域（技能库在就说明查得到、写得动）。"""
    got: set[str] = set()
    for root in (DATA / "skills", WS / "skills"):
        if not root.is_dir():
            continue
        for d in root.iterdir():
            n = d.name.lower()
            for k, fam in SKILL_DOMAINS.items():
                if k in n:
                    got.add(fam)
    return got


def _clean(s: str) -> str:
    s = re.sub(r"^[-*]\s*", "", s.strip())
    s = re.sub(r"^\d{1,2}:\d{2}\s*(\[[^\]]*\])?\s*", "", s)
    s = s.replace("**", "").replace("`", "")
    s = re.split(r"\s*\(来源:", s)[0]
    return s.strip()


BAD = ("|", "File written", "Command completed", "exit code", "Select-String",
       "python.exe", "Get-Content", "Copy-Item", "Get-ChildItem")


def _ok(s: str) -> bool:
    if any(b in s for b in BAD):
        return False
    if s[:1] in "（(【":
        return False
    zh = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return zh / max(1, len(s)) >= 0.35


def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 热点
SENT_SPLIT = re.compile(r"(?<=[。！？；])")


def _hot_terms(limit: int = 40) -> list[str]:
    """热点词：self/hotwords.txt 优先（我检索后手写），没有才退回外部料标题。

    hotwords 是手挑的方向词，语义干净；inbox 标题里混着结论句、新闻长句，
    混在一起随机抽，会抽到「某种细菌会吃你的肠子」这种根本不是热点的东西。
    """
    names: list[str] = []
    if HOTFILE.is_file():
        try:
            for ln in HOTFILE.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                s = ln.strip().lstrip("-*").strip()
                if not s or s.startswith("#"):
                    continue
                names.append(s[:28])
        except Exception:
            pass
    if names:
        return names

    out: list[str] = []
    d = SELF / "info"
    if d.is_dir():
        for f in sorted(d.glob("inbox_*.jsonl"))[-2:]:
            try:
                raw = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            for ln in raw:
                try:
                    t = _clean(str(json.loads(ln).get("title") or ""))
                except Exception:
                    continue
                for s in SENT_SPLIT.split(t):
                    s = s.strip(" ·—-|")
                    if 6 <= len(s) <= 20 and _ok(s) and s not in out:
                        out.append(s)
                    if len(out) >= limit * 2:
                        break
    random.shuffle(out)
    return out[:limit]


def domain_of(text: str) -> str | None:
    best, hits = None, 0
    for dom, kws in DOMAINS.items():
        n = sum(1 for k in kws if k.lower() in text.lower())
        if n > hits:
            best, hits = dom, n
    return best


def prob(dims: list[str]) -> float:
    """这个组合做出来的概率——粗估，只用来决定「还要不要再碰一维」。

    高的来源：① 有现成技能库/工具（查得到、跑得动）② 维度同族（近亲）
    低的来源：跨族维度越多，越是没人干过（也可能是没人能干）
    """
    p = 0.52
    fams = {FAMILY.get(d, "工程") for d in dims}
    p -= 0.15 * (len(fams) - 1)                       # 每多跨一族降一档
    have = living_skills()
    p += 0.12 * min(2, sum(1 for d in dims if d in have))
    if len(dims) >= 3:
        p -= 0.05                                      # 三维本身就更难落地
    return round(max(0.06, min(0.88, p)), 2)


def _far_domain(taken: list[str], prefer: list[str]) -> str:
    """选下一个维度：优先换族，其次靠前的领域。"""
    fams = {FAMILY.get(d, "工程") for d in taken}
    pool = [d for d in prefer if d not in taken]
    far = [d for d in pool if FAMILY.get(d, "工程") not in fams]
    return random.choice(far or pool or list(DOMAINS))


HINT_HI = "先查有没有先行者：这个组合大概率有人做过，找到最接近的一版，改动它的一个变量。"
HINT_MID = "把它压到最小可跑：只做一个载体上的三分钟样本，能听/能看/能算就行。"
HINT_LO = "先查先行者再决定做不做——跨族太远，别一上来就搭全套，先证明「机制搬得过去」。"

# 第三维候选（低概率才加）：只许挑「真能搭上话」的领域，
# 不然会碰出「游戏NPC × 动漫 × 量化」这种听着神其实没入口的组合。
THIRD_PREF = ["模型", "美术", "音乐", "语音", "自动化", "数据", "工程", "测试", "文本", "教学"]

# 第一维候选：热点没自带领域时从这里抽（创作/工程为主，量化硬件这类要热点自己带才碰）
FIRST_PREF = ["影视", "美术", "音乐", "模型", "工程", "动漫", "语音", "自动化", "数据", "文本"]

# 表达域（做给人看/听的）与 技术域（怎么实现）——热点自己不带领域时，
# 第一维给表达域、第二维给技术域，撞出来的才像「用 X 做 Y」而不是两个技术词干瞪眼
EXPRESS = ["美术", "音乐", "影视", "文本", "动漫", "教学", "语音"]
TECH = ["模型", "工程", "自动化", "数据", "测试"]


def make(dry: bool = False) -> int:
    hots = _hot_terms()
    if not hots:
        print("[skip] 没有热点词（hotwords.txt 和 info/inbox 都空）")
        return 1
    hot = random.choice(hots)
    have = living_skills()
    # 热点自带领域就沿用它，否则抽一个；第二维优先挑「有积累」的
    d1 = domain_of(hot)
    if d1:
        # 热点自带领域：另一维优先换族
        d2_pool = [d for d in THIRD_PREF + FIRST_PREF if d != d1]
    else:
        # 热点只是个趋势词：一维给表达载体，一维给技术手段
        d1 = random.choice(EXPRESS)
        d2_pool = [d for d in TECH if d != d1] or list(DOMAINS)
    d2 = _far_domain([d1], d2_pool or list(DOMAINS))

    dims = [d1, d2]
    p = prob(dims)
    # 主人规则：碰一个概率越低，就多碰一个（加维只能更不确定，不许反而变容易）
    if p <= 0.38:
        third = _far_domain(dims, THIRD_PREF)
        if third not in dims:
            dims.append(third)
            p = min(prob(dims), round(p - 0.05, 2))

    p = prob(dims)
    hint = HINT_LO if p < 0.2 else (HINT_MID if p < 0.45 else HINT_HI)
    title = f"灵感 p={p}｜{hot[:18]} × ({'+'.join(dims)})"
    iid = "insp_" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    item = {
        "id": iid,
        "kind": "inspiration",
        "title": title,
        "keyword": f"{hot[:20]} / {' / '.join(dims)}",
        "curiosity_cat": "灵感",
        "priority": 5,          # 与好奇同级：可供给其他轮次挑，但不是必须来源
        "cost_rmb": 0,
        "source": f"碰撞：热点×{'×'.join(dims)}（p={p}，{len(dims)}维）",
        "source_url": "",
        "ttl_h": TTL_H,
        "added_at": now_s(),
        "tier": "t2_write_low",
        "hot": hot,
        "domains": dims,
        "dims": len(dims),
        "prob": p,
        "consumable": True,
        "verify": (
            f"按 self/linkage.md 第六节做一次灵感验证。热点（{hot}）× 领域（{'、'.join(dims)}），"
            f"粗估实现概率 {p}（{len(dims)} 维）。第一步先写一句「如果 {hot[:14]} 的机制像 {dims[0]}，"
            f"那么在 {dims[-1]} 上 C 应该可测」——C 得是指标或现象，不许是形容词。"
            f"第二步按这个做：{hint} 结果（成立·不成立·测不了）和为什么，记进 self/inspiration.md。"
        ),
    }

    if dry:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0

    doc = load_pool()
    live = live_inspiration(doc)
    if len(live) >= MAX_LIVE:
        print(f"[skip] 池里还有 {len(live)} 条未验证灵感，先消化再加")
        return 0
    for it in live:
        if it.get("id") == iid:
            print(f"[skip] 这条撞过了：{iid}")
            return 0

    doc["items"].append(item)
    doc["items"] = doc["items"][-20:]
    POOL.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    if not LEDGER.exists():
        LEDGER.write_text(
            "# 灵感台账（待验证 → 已定性）\n\n"
            "> 规格见 `self/linkage.md` 第六节。每条必须有可做可撤回的实验和结论。\n\n",
            encoding="utf-8")
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(f"- [{now_s()}] 入池 `{iid}` **{title}**\n"
                f"  - 热点：{hot}\n"
                f"  - 维度：{' / '.join(dims)}（跨族={len({FAMILY.get(x, '工程') for x in dims})}）p={p}\n"
                f"  - 待写：如果 A 像 B，那么 C？ / 实验： / 结果：\n")
    print(f"[ok] {iid} {title}")
    return 0


def load_pool() -> dict:
    try:
        d = json.loads(POOL.read_text(encoding="utf-8-sig"))
        if isinstance(d, dict):
            d.setdefault("items", [])
            return d
    except Exception:
        pass
    return {"generated_at": now_s(), "items": []}


def live_inspiration(doc: dict) -> list[dict]:
    return [it for it in (doc.get("items") or [])
            if isinstance(it, dict) and it.get("kind") == "inspiration"]


def show() -> int:
    doc = load_pool()
    live = live_inspiration(doc)
    print(f"池内灵感 {len(live)} 条（上限 {MAX_LIVE}）：")
    for it in live:
        print(f"  - {it.get('id')} p={it.get('prob', '?')} {it.get('title')}")
    if LEDGER.exists():
        rows = LEDGER.read_text(encoding="utf-8").splitlines()
        print(f"\n台账 {len(rows)} 行，末 12 行：")
        for ln in rows[-12:]:
            print("  " + ln)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--show" in args:
        return show()
    return make(dry="--dry" in args)


if __name__ == "__main__":
    raise SystemExit(main())
