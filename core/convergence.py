#!/usr/bin/env python3
"""自迭代收敛护栏。

两个病：
1. 同类动作反复中选、反复零产出（explore 12 次全空），靠 stale 上浮又爬回来；
2. 同一个目标 id 一天里被选好几次，等于原地打转。

做法：不改世界、不拦选事，只从 jumps 账本（jev/rounds.jsonl）读历史，
给「高分空转的 kind」和「今天已被选过两次的 id」加惩罚分。
全部逻辑在当前进程内算，坏了也只影响排序（调用方 try/except 静默）。

判据（写死在代码里，改之前先看 test_convergence.py）：
- 高分空转：p_top ≥ 0.70 且 y < 0.5
- kind 空转累计 ≥ 2 才开始扣分，≥ 3 扣满
- 同一 goal id 24 小时内出现 ≥ 2 次，按次数线性扣，封顶 -24
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
ROUNDS = SELF / "jev" / "rounds.jsonl"
DECISIONS = SELF / "decisions.jsonl"

HIGH_P = 0.70          # 绝对阈值。JEV 把温度 T 升到 20 之后 p_top 被压到 0.18~0.5，
                       # 单靠它 waste 永远记不到，惩罚就永远不会出手。
HIGH_P_REL = 1.6       # 相对阈值：p_top ≥ 均匀概率 × 1.6 也算"高分"（2026-11-17 0527 加）
LOW_Y = 0.5
WASTE_SOFT = 2
WASTE_HARD = 3
PENALTY_SOFT = -4.0
PENALTY_HARD = -8.0
SAME_ID_WINDOW_H = 24
SAME_ID_MERGE_S = 60    # 60 秒内的同 id 多行归并成一次（2026-11-17 二次收紧：
                        # 原 600s。--show 的写闸补上之后，账本里不再有观测刷屏，
                        # 剩下的秒级重复只是同一轮重入；600s 反而把真实连续中选
                        # 一起吞了 —— 压测 50 轮里同 id 中 9 次、罚分 0 次即是此故）
SAME_ID_LIMIT = 2
SAME_ID_STEP = -12.0
SAME_ID_CAP = -24.0
STOP_STREAK = 5

_CACHE: dict = {"mtime": None, "rows": []}


def _load_rounds() -> list[dict]:
    try:
        m = ROUNDS.stat().st_mtime
    except OSError:
        return []
    if _CACHE["mtime"] == m:
        return _CACHE["rows"]
    rows: list[dict] = []
    try:
        for line in ROUNDS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    except Exception:
        rows = []
    _CACHE.update({"mtime": m, "rows": rows})
    return rows


def _y(r: dict) -> float | None:
    for k in ("human_y", "outcome"):
        v = r.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _chosen(r: dict) -> dict:
    ch = r.get("chosen") or r.get("actual_id")
    for o in r.get("options") or []:
        if o.get("id") == ch:
            return o
    opts = r.get("options") or []
    return opts[0] if opts else {}


def _kind(r: dict) -> str:
    return str(_chosen(r).get("kind") or "unknown")


def _ts(r: dict) -> datetime | None:
    try:
        return datetime.strptime(str(r.get("ts")), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _high_p(r: dict) -> bool:
    """这轮算不算"高分中选"：绝对 0.70，或明显高于均匀分布（p_top ≥ 1/n × 1.6）。

    两条命中一条即可。单项池 / 双项池在均匀时 p_top=1.0/0.5，相对阈值在 n<3 时
    直接判否，避免把"池里只有两条、本来就没得选"记成空转。
    """
    p = r.get("p_top")
    if p is None:
        return False
    try:
        p = float(p)
    except (TypeError, ValueError):
        return False
    if p >= HIGH_P:
        return True
    n = r.get("shown") or len(r.get("options") or [])
    try:
        n = int(n)
    except (TypeError, ValueError):
        return False
    if n < 3:
        return False
    return p >= HIGH_P_REL / n


def waste_by_kind(rows: list[dict] | None = None, window: int = 40) -> dict[str, int]:
    """近 window 条里，各 kind 的高分空转次数。"""
    rows = (rows if rows is not None else _load_rounds())[-window:]
    out: dict[str, int] = {}
    for r in rows:
        y = _y(r)
        if y is None:
            continue
        if _high_p(r) and y < LOW_Y:
            k = _kind(r)
            out[k] = out.get(k, 0) + 1
    return out


def kind_penalty(kind: str | None, rows: list[dict] | None = None) -> float:
    if not kind:
        return 0.0
    w = waste_by_kind(rows).get(str(kind), 0)
    if w >= WASTE_HARD:
        return PENALTY_HARD
    if w >= WASTE_SOFT:
        return PENALTY_SOFT
    return 0.0


def _recent_same_id(gid: str, rows: list[dict]) -> int:
    """24 小时内同一条 id 被判成"又选了它"的次数。

    10 分钟归并（2026-11-17 修）：同一段时间里写进来的多行只算一次。此前每行都算，
    一次循环跑或一次误写就能把 n 顶到几十上百，直接把候选扣到 -24 永不见天日。
    真实轮转间隔远大于 10 分钟，归并不会吃掉正常重复。

    只算结账行（2026-11-17 20:0x 再修）：outcome/human_y 为空的行是"只决策没落地"
    ——`--commit` 连环刷屏 3 分钟写了 1311 行、`--show` 观测也算在内，它们不代表
    这条 id 真被选去干了活。计入就会让 42/82 个候选只剩 -24 的坟头分。
    """
    now = datetime.now()
    buckets: set = set()
    for r in rows:
        if _y(r) is None:      # 没结账 = 不是一次真实中选，不参与「反复踩同一个坑」的计数
            continue
        t = _ts(r)
        if t is None or (now - t) > timedelta(hours=SAME_ID_WINDOW_H):
            continue
        if str(r.get("actual_id") or r.get("chosen")) == str(gid):
            buckets.add(int(t.timestamp() // SAME_ID_MERGE_S))
    return len(buckets)


def id_penalty(gid: str | None, rows: list[dict] | None = None) -> float:
    if not gid:
        return 0.0
    rows = rows if rows is not None else _load_rounds()
    n = _recent_same_id(str(gid), rows)
    if n < SAME_ID_LIMIT:
        return 0.0
    return max(SAME_ID_CAP, SAME_ID_STEP * (n - SAME_ID_LIMIT + 1))


def pick_penalty(kind: str | None, gid: str | None) -> float:
    """给 iter_cycle.pick 用的一行接口。任何异常都返回 0，绝不打断选事。"""
    try:
        rows = _load_rounds()
        return kind_penalty(kind, rows) + id_penalty(gid, rows)
    except Exception:
        return 0.0


def should_stop(rows: list[dict] | None = None) -> dict:
    """连续 STOP_STREAK 轮拿到低于 LOW_Y 的 y，就建议停下让主人复核。"""
    rows = rows if rows is not None else _load_rounds()
    tail = [r for r in rows if _y(r) is not None][-STOP_STREAK:]
    if len(tail) < STOP_STREAK:
        return {"stop": False, "n": len(tail)}
    ys = [float(_y(r)) for r in tail]
    if all(y < LOW_Y for y in ys):
        return {"stop": True, "reason": f"最近 {STOP_STREAK} 轮全部低产出 {ys}", "n": len(ys)}
    return {"stop": False, "n": len(ys)}


def snapshot() -> dict:
    rows = _load_rounds()
    return {
        "rounds": len(rows),
        "waste_by_kind": waste_by_kind(rows),
        "kind_penalty": {k: kind_penalty(k, rows) for k in waste_by_kind(rows)},
        "stop": should_stop(rows),
        "decisions_file": str(DECISIONS) if DECISIONS.exists() else None,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
