"""组合级疲劳（主人 2026-11-17 20:15 定口径）。

规则：
  · 疲劳的单位是「组合」，不是单条。
  · 某个组合在 24h 内出现过 → 这个组合不再出现（会被剔除成员直到它变成新组合）。
  · 比历史组合「多一条」的算新组合：{1,2} 出现过，{1,2,3} 不算疲劳，可以出现。
  · 缩水的（{1,2,3} 出现过，本轮只有 {1,2}）算旧组合，疲劳。
  · 单条（组合大小 < 2）永不判疲劳，否则一个类会被永久锁死。

实现口径（怎么算「出现过」）：
  同 kind（同类条目）在同一轮候选表里同框，记成一个组合，落到 jev/rounds.jsonl 的 options 里。
  所以历史证据直接读 rounds.jsonl，窗口 = 最近 24h。

补丁层（主人 2026-11-17 20:37 选 A）：单条出场配额。
组合疲劳会把同类砍到只剩 1 条，剩下那条就成了常驻台柱（实测 insp_63a81cff 20/50 轮在候选表里），
所以再加一道：同一个 id 24h 内进候选表满 APPEAR_CAP(=4) 次，本轮就不进池。
配额不是判据，超了就退，不改变已有分数。

对外：
  load_sets(path, hours=24)        -> {kind: [set(id), ...]}
  apply(pool, hist)                -> (pool, drops)
  load_appearances(path, hours=24) -> {id: 次数}
  apply_cap(pool, counts, cap)     -> (pool, drops)
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

CST = datetime.timezone(datetime.timedelta(hours=8))
WINDOW_H = 24.0
MIN_SET = 2          # 组合至少 2 条才算一个组合
APPEAR_CAP = 4       # 同一个 id 24h 内最多进候选表几次（主人 2026-11-17 选 A）
SCOPE = ("inspiration",)  # 疲劳只管这几类（主人 2026-11-17 20:48：只在灵感轮次内实现）
MAX_LINES = 600      # 只扫最近这么多行，rounds.jsonl 会越长越慢


def _parse_ts(ts) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
    except Exception:
        return None


def load_sets(path, hours: float = WINDOW_H, now: datetime.datetime | None = None) -> dict:
    """读 rounds.jsonl，返回 {kind: [组合set, ...]}，只留窗口内、大小≥MIN_SET 的组合。"""
    now = now or datetime.datetime.now(CST)
    p = Path(path)
    out: dict[str, list[set]] = {}
    if not p.exists():
        return out
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LINES:]
    except Exception:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        t = _parse_ts(r.get("ts"))
        if t is None or (now - t).total_seconds() > hours * 3600:
            continue
        by_kind: dict[str, set] = {}
        for o in (r.get("options") or []):
            k = str(o.get("kind") or "")
            i = o.get("id")
            if i:
                by_kind.setdefault(k, set()).add(str(i))
        for k, s in by_kind.items():
            if len(s) >= MIN_SET:
                out.setdefault(k, []).append(s)
    return out


def is_fatigued(kind: str, ids, hist: dict) -> bool:
    """这组条目算不算旧组合：本轮集合 ⊆ 某条历史集合（即没有新增条目）→ 疲劳。"""
    s = {str(i) for i in ids}
    if len(s) < MIN_SET:
        return False
    for h in hist.get(str(kind), []):
        if s <= h:
            return True
    return False


def apply(pool, hist: dict) -> tuple[list, list]:
    """在已打分的候选池上做疲劳剔除。

    pool: [(score, goal)]；同 kind 里只要组合命中疲劳，就砍掉该组最低分的，
    砍到不再是旧组合（或只剩 1 条）为止。目的是逼它换搭档/加新条目，而不是整类歇掉。
    返回 (新pool, drops)，drops 用于落进 decision 的由头里，事后能查为什么少了。
    """
    if not pool or not hist:
        return pool, []
    groups: dict[str, list[int]] = {}
    for idx, (_sc, g) in enumerate(pool):
        groups.setdefault(str(g.get("kind") or ""), []).append(idx)

    drop_idx: set[int] = set()
    drops: list[tuple[str, str, str]] = []
    for kind, idxs in groups.items():
        if SCOPE and kind not in SCOPE:      # 疲劳只在灵感这类里生效，别的池子不砍
            continue
        cand = sorted(idxs, key=lambda i: pool[i][0])  # 低分在前，先砍低分
        while len([i for i in cand if i not in drop_idx]) >= MIN_SET:
            alive = [i for i in cand if i not in drop_idx]
            ids = [pool[i][1].get("id") for i in alive]
            if not is_fatigued(kind, ids, hist):
                break
            victim = alive[0]
            drop_idx.add(victim)
            drops.append((kind, str(pool[victim][1].get("id")),
                          f"{kind} 组合 24h 内出现过（{len(ids)}条），砍最低分换组合"))

    if not drop_idx:
        return pool, []
    kept = [(sc, g) for i, (sc, g) in enumerate(pool) if i not in drop_idx]
    return (kept or pool), drops


def load_appearances(path, hours: float = WINDOW_H,
                     now: datetime.datetime | None = None) -> dict:
    """统计窗口内每个 id 进过几次候选表（= 出现在 options 里的轮数）。"""
    now = now or datetime.datetime.now(CST)
    p = Path(path)
    out: dict[str, int] = {}
    if not p.exists():
        return out
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LINES:]
    except Exception:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        t = _parse_ts(r.get("ts"))
        if t is None or (now - t).total_seconds() > hours * 3600:
            continue
        for i in {str(o.get("id")) for o in (r.get("options") or []) if o.get("id")}:
            out[i] = out.get(i, 0) + 1
    return out


def apply_cap(pool, counts: dict, cap: int = APPEAR_CAP) -> tuple[list, list]:
    """出场配额：24h 内进候选表满 cap 次的 id，本轮不进池（只管 SCOPE 里的类）。

    超配额的全砍光时，留次数最少的那批——配额防台柱，不是防停摆。
    返回 (新pool, drops)，drops=[(id, 已出现次数), ...]
    """
    if not pool or not counts or cap <= 0:
        return pool, []
    kept, drops = [], []
    for sc, g in pool:
        if SCOPE and str(g.get("kind") or "") not in SCOPE:   # 配额跟组合疲劳同一个作用域
            kept.append((sc, g))
            continue
        i = str(g.get("id"))
        n = int(counts.get(i, 0) or 0)
        if n >= cap:
            drops.append((i, n))
            continue
        kept.append((sc, g))
    if drops and not any(not SCOPE or str(g.get("kind") or "") in SCOPE for _s, g in kept):
        # 作用域内被砍空了：只留最少次数那批，让轮换照样转
        alive_pool = [(sc, g) for sc, g in pool
                      if not SCOPE or str(g.get("kind") or "") in SCOPE]
        least = min(int(counts.get(str(g.get("id")), 0) or 0) for _s, g in alive_pool)
        keep_min = [(sc, g) for sc, g in alive_pool
                    if int(counts.get(str(g.get("id")), 0) or 0) == least]
        if keep_min:
            back = {str(g.get("id")) for _s, g in keep_min}
            return kept + keep_min, [d for d in drops if d[0] not in back]
    return kept, drops
