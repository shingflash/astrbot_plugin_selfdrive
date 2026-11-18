# -*- coding: utf-8 -*-
"""方向脑：方向盘 + 禁做通道 + 跑偏收口判据。

- direction.md    给人读的方向（主人改正文，我只看不改）
- direction_rules.json  机器读的加减分表
- veto.txt        禁做通道，命中即拦

用法：
  python direction.py show
  python direction.py check "标题" --kind curiosity
  python direction.py drift
  python direction.py veto "要发给客户的话"
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
DIRECTION = SELF / "direction.md"
RULES = SELF / "direction_rules.json"
VETO = SELF / "veto.txt"
LEDGER = SELF / "jev" / "ledger.jsonl"
DRIFT_STATE = SELF / "direction_drift.json"
_ROWS_CACHE: tuple | None = None
_RULES_CACHE: tuple | None = None

DEFAULT_RULES = {
    "boost": {
        "工具": 2.0, "脚本": 1.5, "测试": 1.5, "验证": 1.0, "复用": 1.5,
        "客户": 2.0, "订单": 2.0, "报价": 1.5, "交付": 2.0, "打印": 1.5,
        "修": 1.0, "缺陷": 1.5, "bug": 1.5, "产能": 1.5,
    },
    "penalty": {
        "自省": -1.5, "复盘": -1.0, "想法": -1.0, "感悟": -1.5,
        "新闻": -1.0, "看视频": -0.5, "再读": -1.0, "整理": -0.5,
    },
    "drift": {"window": 3, "thr": 2, "freeze_on_hit": True},
}


def _load_rules() -> dict:
    """读规则表，按 mtime 缓存 —— 打分时每个候选都要 stage/side，不能每次都摸磁盘。"""
    global _RULES_CACHE
    try:
        sig = RULES.stat().st_mtime_ns
    except Exception:
        return dict(DEFAULT_RULES)
    if _RULES_CACHE and _RULES_CACHE[0] == sig:
        return _RULES_CACHE[1]
    try:
        d = json.loads(RULES.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_RULES)
    for k, v in DEFAULT_RULES.items():
        d.setdefault(k, v)
    _RULES_CACHE = (sig, d)
    return d


def ensure_rules() -> Path:
    if not RULES.exists():
        RULES.write_text(json.dumps(DEFAULT_RULES, ensure_ascii=False, indent=2), encoding="utf-8")
    return RULES


def load_veto() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not VETO.exists():
        return out
    for line in VETO.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kw, _, why = line.partition("|")
        kw = kw.strip()
        if kw:
            out.append((kw, why.strip()))
    return out


def veto_hit(text: str) -> dict | None:
    """命中返回 {kw, why}，否则 None。子串匹配，不看大小写。"""
    t = (text or "").lower()
    for kw, why in load_veto():
        if kw.lower() in t:
            return {"kw": kw, "why": why or "veto.txt 命中"}
    return None


def direction_boost(title: str, kind: str = "", analysis: str = "") -> dict:
    """按加减分表给方向分（不是绝对正确，只是把方向变成能算的数）。

    三层叠加：
      1) kind 基础分  —— 动作类型本身离「进化/赚钱」多远（主干，权重最大）
      2) 加分关键词  —— 说明这一轮真的落到可复用产物/真实需求上
      3) 减分关键词  —— 白干特征
    """
    rules = _load_rules()
    kb = rules.get("kind_boost", {})
    hits: list[str] = []
    score = 0.0
    base = float(kb.get(str(kind or ""), 0.0) or 0.0)
    if base:
        score += base
        hits.append(f"kind {kind}:{base:+}")
    text = f"{title} {analysis}".lower()
    for kw, w in rules.get("boost", {}).items():
        if kw.lower() in text:
            score += float(w)
            hits.append(f"+{w}:{kw}")
    for kw, w in rules.get("penalty", {}).items():
        if kw.lower() in text:
            score += float(w)
            hits.append(f"{w}:{kw}")
    # 上限：方向分只该「调序」，不该反超成本与收敛护栏（±10 封顶）
    score = max(-10.0, min(10.0, score))
    return {"boost": round(score, 2), "hits": hits}


def check(title: str, kind: str = "", analysis: str = "") -> dict:
    # veto 只看标题/正文的真实字面，不看 kind：kind 里出现「转账」之类的词不该误杀
    v = veto_hit(title)
    if v:
        return {"verdict": "veto", "reason": f"命中禁做「{v['kw']}」：{v['why']}", "boost": None}
    b = direction_boost(title, kind, analysis)
    verdict = "pass" if b["boost"] >= 0 else "warn"
    return {"verdict": verdict, "reason": f"方向分 {b['boost']:+}", **b}


def _ledger_rows() -> list[dict]:
    """读账本，按 (mtime, size) 缓存 —— pick 每轮都要算节奏，热路径不能反复整文件读。"""
    global _ROWS_CACHE
    try:
        sig = (LEDGER.stat().st_mtime_ns, LEDGER.stat().st_size)
    except Exception:
        return []
    if _ROWS_CACHE and _ROWS_CACHE[0] == sig:
        return _ROWS_CACHE[1]
    rows = []
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    _ROWS_CACHE = (sig, rows)
    return rows


def stage_of(kind: str) -> str:
    """把 kind 归到「输入 / 消化 / 产出」三段。未知 kind 按产出一类（不奖励白看）。"""
    return (_load_rules().get("stage") or {}).get(str(kind or ""), "output")


def balance() -> dict:
    """节奏判据（主人 2026-11-17）：输入/消化/产出三段平级，但不许一直只干一种。

    - 某一段在最近 window 轮里占比 >= over：罚（垄断段落会把整轮打成同一件事）
    - 完全没出现的段落：奖（缺哪补哪）
    - 同一段连续 same_streak 轮以上：额外罚（纯梗：连吃六顿白饭也会腻）
    只读账本，不写任何东西；账本不够长就返回空（不硬判）。
    """
    rules = _load_rules().get("balance", {})
    win = int(rules.get("window", 6))
    over = float(rules.get("over", 0.66))
    over_pen = float(rules.get("over_pen", -2.5))
    miss_boost = float(rules.get("miss_boost", 2.0))
    streak_pen = float(rules.get("same_streak_pen", -1.0))
    min_tail = int(rules.get("min_tail", 4))
    rows = [r for r in _ledger_rows() if r.get("kind")]
    rows.sort(key=lambda r: str(r.get("ts") or r.get("at") or ""))
    stages = [stage_of(r.get("kind")) for r in rows[-win:]]
    out: dict = {"tail": stages, "adj": {}, "note": ""}
    if len(stages) < min_tail:
        out["note"] = f"账本只有 {len(stages)} 轮，样本不够不判节奏"
        return out
    cnt = {s: stages.count(s) for s in set(stages)}
    total = len(stages)
    adj = out["adj"]
    for st, c in cnt.items():
        if c / total >= over and c >= 3:
            adj[st] = over_pen
    for st in ("input", "digest", "output"):
        if st not in cnt:
            adj[st] = miss_boost
    # 尾部连续同段
    run = 1
    for i in range(len(stages) - 2, -1, -1):
        if stages[i] == stages[-1]:
            run += 1
        else:
            break
    if run >= 3:
        adj[stages[-1]] = adj.get(stages[-1], 0.0) + streak_pen * (run - 2)
    hits = [f"{k}:{v:+.1f}" for k, v in adj.items()]
    out["note"] = f"最近{total}轮 {'/'.join(stages)} → " + ("、".join(hits) if hits else "节奏正常")
    return out


def side_of(kind: str) -> str:
    """半双工用：A=输入侧（含消化），B=产出侧。消化是内部处理，两侧都通（'*'）。"""
    st = stage_of(kind)
    if st == "digest":
        return "A"      # 消化是输入的下游处理，仍属「读与想」那一侧
    return "A" if st == "input" else "B"


def half_duplex() -> dict:
    """半双工（主人 2026-11-17）：上一轮占了哪一侧，这一轮就轮到另一侧。

    比的是**上一轮实际跑的 kind**，不是猜的；两侧完全平级，
    只约束「同一时间只跑一个方向」，不预设谁先谁后，也不让任一侧长期占线。
    消化(digest)归输入侧（读与想是一条线），产出单独一条线。
    只软调序（不硬禁），池子缺人就照常跑，绝不空转。
    """
    rules = _load_rules().get("duplex", {})
    hold_pen = float(rules.get("hold_pen", 3.0))   # 还占着线的另一侧
    turn_boost = float(rules.get("turn_boost", 1.5))  # 轮到它的那一侧
    rows = [r for r in _ledger_rows() if r.get("kind")]
    rows.sort(key=lambda r: str(r.get("ts") or r.get("at") or ""))
    last = side_of(rows[-1].get("kind")) if rows else ""
    adj = {}
    if last in ("A", "B"):
        other = "B" if last == "A" else "A"
        adj[last] = -hold_pen
        adj[other] = turn_boost
    return {"last_side": last, "adj": adj,
            "note": f"上一轮在{'输入' if last == 'A' else ('产出' if last == 'B' else '?')}侧 → "
                    f"本轮轮到{'产出' if last == 'A' else '输入'}侧" if last else "账本空，先手不限"}


def drift() -> dict:
    """连续 window 轮都低分（score<=thr）就收口。只算已有 score 的轮。"""
    rules = _load_rules().get("drift", {})
    win = int(rules.get("window", 3))
    thr = float(rules.get("thr", 2))
    scored = [r for r in _ledger_rows() if r.get("score") is not None]
    scored.sort(key=lambda r: str(r.get("ts") or r.get("at") or ""))
    tail = scored[-win:]
    low = [r for r in tail if float(r["score"]) <= thr]
    frozen = bool(rules.get("freeze_on_hit", True)) and len(tail) >= win and len(low) == win
    out = {
        "freeze": frozen,
        "window": win,
        "thr": thr,
        "tail": [{"ts": r.get("ts"), "score": r.get("score"), "title": (r.get("title") or "")[:40]} for r in tail],
        "todo": "只准修 bug / 写测试 / 只读巡检，不许抽新选题；并请主人补一句方向" if frozen else "",
    }
    try:
        DRIFT_STATE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return out


def show() -> str:
    n = len(load_veto())
    d = drift()
    return (f"direction.md {'在' if DIRECTION.exists() else '缺'} | veto {n} 条 | "
            f"收口={'是' if d['freeze'] else '否'} (窗口{d['window']} 阈值{d['thr']})")


def main(argv: list[str]) -> int:
    ensure_rules()
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "show":
        print(json.dumps({"ok": True, "line": show()}, ensure_ascii=False))
    elif cmd == "check":
        title = argv[2] if len(argv) > 2 else ""
        kind = ""
        if "--kind" in argv:
            i = argv.index("--kind")
            kind = argv[i + 1] if i + 1 < len(argv) else ""
        print(json.dumps(check(title, kind), ensure_ascii=False))
    elif cmd == "veto":
        text = argv[2] if len(argv) > 2 else ""
        hit = veto_hit(text)
        print(json.dumps({"hit": hit, "blocked": bool(hit)}, ensure_ascii=False))
    elif cmd == "drift":
        print(json.dumps(drift(), ensure_ascii=False))
    else:
        print(json.dumps({"error": f"unknown cmd {cmd}"}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
