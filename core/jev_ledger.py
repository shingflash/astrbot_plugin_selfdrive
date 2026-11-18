# -*- coding: utf-8 -*-
"""Jev 打分台账（2026-11-17 主人重定义，取代温度拟合那一套）。

一条 = 一轮的完整记账，字段固定六个：
  池名 / 题目 / 轮次的分析 / 实现方式 / 实现结果 / 标注分数(0-10)

分数语义（主人原话）：
  0     空转——做了等于没做
  1-5   可能有用
  6-10  有用

标注来源两种：
  by="self"  自评分（弱标签，权重 1）
  by="owner" 人工标注（权重 3，同一轮同时有人工时人工覆盖自评）

回灌：每池按权重算均分 → 下一轮 pick 的价值加分；加权样本不足 MIN_W 时用
iter_cycle.VALUE_BONUS 的先验值，绝不拿 1-2 条样本当真。
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
JEV = SELF / "jev"
LEDGER = JEV / "ledger.jsonl"

CST = timezone(timedelta(hours=8))
W_SELF = 1.0
W_HUMAN = 3.0
MIN_W = 3.0          # 旧阈值（保留兼容）；现在走 SHRINK_K 收缩，不再硬切先验
SHRINK_K = 12.0      # 向先验收缩的强度：等价于"中性先验占 12 个权重的样本量"
NEUTRAL = 4.0        # 收缩目标就固定是中位分 4，不认手设先验（practice 没先验、introspect 先验 8 会把负样本抬起来）
USEFUL = 6           # ≥6 分算「有用」
LEGACY_Y2SCORE = {1.0: 8, 0.5: 5, 0.0: 0}   # 旧 rounds.jsonl 的 outcome → 新 0-10

# kind → 池（与 iter_cycle.POOL_GROUPS 同源；这里独立一份，避免循环导入）
KIND2POOL = {
    "curiosity": "curiosity", "explore": "learn", "inspiration": "inspiration",
    "introspect": "introspect", "meta": "optimize", "optimize": "optimize",
    "practice": "practice", "maintain": "maintain", "archive": "archive",
}
POOLS = ["curiosity", "learn", "inspiration", "introspect", "optimize",
         "practice", "maintain", "archive"]


def now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def pool_for(kind) -> str:
    k = str(kind or "")
    return KIND2POOL.get(k, k or "unknown")


def band(score) -> str:
    """分数 → 人话档位。"""
    if score is None:
        return "未标"
    s = float(score)
    if s <= 0:
        return "空转"
    if s < 6:
        return "可能有用"
    return "有用"


def clamp(score, by: str = "self") -> int | None:
    """把标注规整成 0-10 整数；None/空 表示未标。"""
    if score is None or str(score).strip() == "":
        return None
    v = int(round(float(score)))
    return max(0, min(10, v))


def read(path: Path | None = None) -> list[dict]:
    p = path or LEDGER
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _write(rows: list[dict], path: Path | None = None) -> None:
    p = path or LEDGER
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _key(r: dict) -> tuple:
    return (str(r.get("ts") or ""), str(r.get("pool") or ""), str(r.get("title") or "")[:40])


def append(rec: dict, path: Path | None = None) -> dict:
    """追加一条；同 (ts,池,题) 已存在则合并（人工覆盖自评，空字段补齐）。"""
    rows = read(path)
    rec = dict(rec)
    rec.setdefault("ts", now())
    rec.setdefault("src", "live")
    rec["score"] = clamp(rec.get("score"), rec.get("by") or "self")
    rec.setdefault("by", "self" if rec["score"] is not None else None)
    rec.setdefault("at", now() if rec["score"] is not None else None)
    for i, old in enumerate(rows):
        if _key(old) == _key(rec):
            merged = dict(old)
            keep_owner = (old.get("by") == "owner" and rec.get("by") != "owner"
                          and old.get("score") is not None)
            for k, v in rec.items():
                if v not in (None, "") and k not in ("ts", "pool", "title"):
                    # 人工标注永远压过自评：分数、标者、标注时间一起锁住
                    if keep_owner and k in ("score", "by", "at"):
                        continue
                    merged[k] = v
            for k in ("analysis", "impl", "result"):
                if not merged.get(k) and rec.get(k):
                    merged[k] = rec[k]
            rows[i] = merged
            _write(rows, path)
            return merged
    rows.append(rec)
    _write(rows, path)
    return rec


def mark(ts: str, score, by: str = "owner", pool: str | None = None,
         path: Path | None = None) -> dict | None:
    """给某轮打 0-10 分。score=None 表示跳过（不写）。"""
    rows = read(path)
    hit = None
    for r in rows:
        if str(r.get("ts")) == str(ts) and (pool is None or r.get("pool") == pool):
            hit = r
            break
    if hit is None:
        return None
    s = clamp(score, by)
    if s is None:
        return hit
    hit["prev_score"] = hit.get("score")
    hit["prev_by"] = hit.get("by")
    hit["score"] = s
    hit["by"] = by
    hit["at"] = now()
    _write(rows, path)
    return hit


def pending(limit: int | None = None, path: Path | None = None) -> list[dict]:
    """还没标分的轮次，最近的在后。"""
    rows = [r for r in read(path) if r.get("score") is None]
    return rows[-limit:] if limit else rows


def stats(path: Path | None = None) -> dict[str, dict]:
    """每池加权统计：n_h 人工条数、n_s 自评条数、w 权重和、avg 加权均分、useful 有用率。"""
    agg: dict[str, list[tuple[float, float]]] = defaultdict(list)
    cnt: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in read(path):
        if r.get("score") is None:
            continue
        if r.get("dup"):
            continue  # 重复轮（同一目标第二次及以后）不计入价值先验，避免"重复"被读成"没用"
        pool = r.get("pool") or pool_for(r.get("kind"))
        w = W_HUMAN if r.get("by") == "owner" else W_SELF
        agg[pool].append((float(r["score"]), w))
        cnt[pool][0 if r.get("by") == "owner" else 1] += 1
    out = {}
    for pool in set(agg) | set(POOLS):
        rows = agg.get(pool) or []
        w = sum(x[1] for x in rows)
        if not rows:
            out[pool] = {"n_h": 0, "n_s": 0, "w": 0.0, "avg": None, "useful": None}
            continue
        avg = sum(s * x for s, x in rows) / w
        useful = sum(x for s, x in rows if s >= USEFUL) / w
        out[pool] = {
            "n_h": cnt[pool][0], "n_s": cnt[pool][1],
            "w": round(w, 1), "avg": round(avg, 2), "useful": round(useful, 2),
        }
    return out


def bonus_for_pool(pool: str, prior: float = 4.0, path: Path | None = None) -> float:
    """池的实测加权均分 → 价值加分；向中性分收缩，样本少自动往 4 分靠。

    bonus = (实测均分*w + 4*K) / (w + K)，K = SHRINK_K。
    收缩目标固定为 4 分，不用 kind 先验：practice 没先验、introspect 先验 8，
    拿它们当目标会把不足的样本反向盖掉。没样本时仍返回 prior（老行为）。
    """
    st = stats(path).get(pool) or {}
    w = float(st.get("w") or 0)
    avg = st.get("avg")
    if w <= 0 or avg is None:
        return float(prior)
    return round((float(avg) * w + NEUTRAL * SHRINK_K) / (w + SHRINK_K), 1)


def bonus_for_kind(kind, prior_map: dict | None = None, path: Path | None = None) -> float:
    prior_map = prior_map or {}
    pool = pool_for(kind)
    prior = prior_map.get(kind, prior_map.get(pool, 4))
    return bonus_for_pool(pool, prior=prior, path=path)


def report(path: Path | None = None) -> str:
    rows = read(path)
    st = stats(path)
    done = [r for r in rows if r.get("score") is not None]
    lines = [f"Jev 台账：{len(rows)} 轮，已标 {len(done)}（人工 "
             f"{sum(1 for r in done if r.get('by')=='owner')} / 自评 "
             f"{sum(1 for r in done if r.get('by')!='owner')}），待标 {len(rows)-len(done)}"]
    ndup = sum(1 for r in rows if r.get("dup") and r.get("score") is not None)
    if ndup:
        lines.append(f"其中重复轮 {ndup} 条已排除（同一目标第二次及以后，不参与均分/有用率）")
    lines.append(f"{'池':<12}{'人工':>4}{'自评':>4}{'权重':>6}{'均分':>6}{'有用率':>7}")
    for pool in POOLS:
        s = st.get(pool) or {}
        avg = "—" if s.get("avg") is None else f"{s['avg']:.1f}"
        use = "—" if s.get("useful") is None else f"{s['useful']:.0%}"
        lines.append(f"{pool:<12}{s.get('n_h',0):>4}{s.get('n_s',0):>4}"
                     f"{s.get('w',0):>6}{avg:>6}{use:>7}")
    return "\n".join(lines)


# ---------------- 旧数据迁移 ----------------

def _kind_of_round(r: dict) -> str:
    cid = r.get("actual_id") or r.get("chosen")
    for o in r.get("options") or []:
        if str(o.get("id")) == str(cid):
            return str(o.get("kind") or "unknown")
    opts = r.get("options") or []
    return str(opts[0].get("kind") or "unknown") if opts else "unknown"


def _labels() -> dict[str, dict]:
    p = JEV / "labels.jsonl"
    out = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("ts") and d.get("y") is not None:
            out[str(d["ts"])] = d
    return out


def migrate_legacy(path: Path | None = None, dry: bool = False) -> dict:
    """把 rounds.jsonl 的 81 条旧轮次迁进台账（数据保留，原文件不动）。"""
    rows = read(path)
    have = {_key(r) for r in rows}
    rp = JEV / "rounds.jsonl"
    if not rp.exists():
        return {"added": 0, "skipped": 0}
    labels = _labels()
    added = skipped = 0
    for line in rp.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("outcome") is None:
            skipped += 1
            continue
        kind = _kind_of_round(r)
        pool = pool_for(kind)
        opts = r.get("options") or []
        y = r.get("outcome")
        by = "self"
        lab = labels.get(str(r.get("ts")))
        if lab is not None:
            y, by = lab.get("y"), "owner"
        rec = {
            "ts": str(r.get("ts")),
            "pool": pool,
            "kind": kind,
            "title": str(r.get("actual_title") or (opts[0].get("title") if opts else "?")),
            "analysis": (f"候选 {len(opts)} 个，当时 p_top={r.get('p_top')}"
                         f"，margin={r.get('margin')}"),
            "impl": "旧口径（温度拟合时代）没有实现方式记录",
            "result": {1.0: "有可验收产出（旧 outcome=1）",
                       0.5: "半用（旧 outcome=0.5）",
                       0.0: "空转，只有标题（旧 outcome=0）"}.get(float(y), f"旧 outcome={y}"),
            "score": LEGACY_Y2SCORE.get(float(y), 5),
            "by": by,
            "at": str(lab.get("at")) if lab else now(),
            "src": "legacy",
        }
        if _key(rec) in have:
            skipped += 1
            continue
        have.add(_key(rec))
        added += 1
        if not dry:
            rows.append(rec)
    if not dry and added:
        _write(rows, path)
    return {"added": added, "skipped": skipped}


if __name__ == "__main__":
    import sys
    if "--migrate" in sys.argv:
        print(json.dumps(migrate_legacy(dry="--dry" not in sys.argv), ensure_ascii=False))
    else:
        print(report())
        print()
        for r in pending(5)[-5:]:
            print(f"{r.get('ts')}  [{r.get('pool')}]  {str(r.get('title'))[:40]}")
