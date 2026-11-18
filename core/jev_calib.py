# -*- coding: utf-8 -*-
"""Jev 记账（2026-11-17 重构后的定位，只干三件事）。

1) log_candidates()：pick 时把候选全集 + 概率落一行到 jev/rounds.jsonl——原始证据，不改。
2) reconcile()：结账时把决策文件对回候选行（谁真被选、多少分），再把这一轮搬进
   jev/ledger.jsonl 打分台账（池名/题目/分析/实现方式/实现结果/分数占位）。
3) label()/report()：写 0-10 标注、出报表；打分与回灌口径全在 jev_ledger.py。

温度拟合（fit_T）已退役：主人 2026-11-17 定的是「按数据打 0-10 分再回灌」，
不是「调温度让 Brier 好看」。Brier 只留作诊断，不再自动改任何参数。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
JEV = SELF / "jev"
ROUNDS = JEV / "rounds.jsonl"
CALIB = JEV / "calib.json"
REPORT = JEV / "report.md"
DEFAULT_T = 6.0   # 只用于把分数摊成「当时把握」给主人看，不参与任何拟合

CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def load_calib() -> dict:
    try:
        d = json.loads(CALIB.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_calib(d: dict) -> None:
    try:
        JEV.mkdir(parents=True, exist_ok=True)
        CALIB.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def softmax(scores, T: float = DEFAULT_T):
    if not scores:
        return []
    m = max(scores)
    t = max(float(T), 1e-6)
    ex = [math.exp((float(s) - m) / t) for s in scores]
    tot = sum(ex)
    return [e / tot for e in ex]


# ---------------- rounds.jsonl（原始证据，格式不变） ----------------

def _read_rounds() -> list[dict]:
    if not ROUNDS.exists():
        return []
    out = []
    for line in ROUNDS.read_text(encoding="utf-8", errors="replace").splitlines():
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


def _write_rounds(rows: list[dict]) -> None:
    JEV.mkdir(parents=True, exist_ok=True)
    ROUNDS.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def log_candidates(pool, top_k: int = 8, T: float | None = None,
                   chosen_id: str | None = None, write: bool = True):
    """iter_cycle.pick() 里调用：pool 是已排序的 [(score, goal), ...]。

    概率在整池上归一（截断只影响记录条数，不影响概率含义），chosen 记实际抽中的 id。

    write=False 只算不落盘：`iter_cycle --show` 是只读观测，看一眼不许往
    rounds.jsonl 追加。原来这里无条件写，每看一次就把同一 id 的"24h 重复次数"
    加一，convergence.id_penalty 被自己的观测刷到封顶 -24，护栏失去区分度
    （2026-11-17 抓：11 分钟 1182 行、同秒 42 行、单 id 24h 216 次）。
    """
    try:
        entries = []
        for sc, g in list(pool):
            entries.append({
                "id": str(g.get("id")),
                "title": (g.get("title") or g.get("keyword") or "")[:60],
                "kind": g.get("kind"),
                "score": round(float(sc), 2),
            })
        if not entries:
            return None
        T = float(T) if T is not None else DEFAULT_T
        ps = softmax([e["score"] for e in entries], T)
        for e, p in zip(entries, ps):
            e["p"] = round(p, 4)
        rows = entries[:top_k]
        chosen = str(chosen_id) if chosen_id else rows[0]["id"]
        c_p = next((e["p"] for e in entries if e["id"] == chosen), round(ps[0], 4))
        rec = {
            "ts": _now(),
            "T": T,
            "pool_total": len(entries),
            "shown": len(rows),
            "options": rows,
            "p_top": rows[0]["p"],
            "margin": round(rows[0]["p"] - (rows[1]["p"] if len(rows) > 1 else 0.0), 4),
            "chosen": chosen,
            "chosen_p": c_p,
            "outcome": None,
        }
        JEV.mkdir(parents=True, exist_ok=True)
        if write:
            with ROUNDS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception:
        return None


# ---------------- 对账 + 搬进台账 ----------------

def _label(score: float) -> float:
    """决策层自评分 → 旧口径 y（保留 rounds.jsonl 的兼容字段）。"""
    if score <= 0:
        return 0.0
    if score < 20:
        return 0.5
    return 1.0


def _find_decision(ts: str, want_id: str | None = None):
    import re
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", str(ts))
    if not m:
        return None
    stem = "decision_" + "".join(m.groups()[:3]) + "_" + "".join(m.groups()[3:])
    hits = sorted((SELF / "iter_out").glob(stem + "*.json"))
    if not hits:
        return None
    if want_id:
        for h in hits:
            try:
                d = json.loads(h.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str((d.get("pick") or {}).get("id")) == str(want_id):
                return d
    try:
        return json.loads(hits[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def reconcile(quiet: bool = False) -> dict:
    """对账：谁真被选 + 把可回执的轮次搬进 ledger（分数留空等标注）。"""
    import jev_ledger as L

    rows = _read_rounds()
    done = miss = 0
    for r in rows:
        if r.get("outcome") is not None:
            continue
        d = _find_decision(r.get("ts", ""), want_id=r.get("chosen"))
        if d is None:
            miss += 1
            continue
        pick = d.get("pick") or {}
        sc = (d.get("score") or {}).get("score")
        if sc is None:
            sc = d.get("decision_score")
        if sc is None:
            for o in (r.get("options") or []):
                if str(o.get("id")) == str(r.get("chosen")):
                    sc = o.get("score")
                    break
        if sc is None:
            miss += 1
            continue
        r["actual_id"] = str(pick.get("id"))
        r["actual_title"] = (pick.get("title") or pick.get("keyword") or "")[:60]
        r["decision_score"] = float(sc)
        r["outcome"] = _label(float(sc))
        r["matched"] = str(pick.get("id")) == str(r.get("chosen"))
        done += 1
    if done:
        _write_rounds(rows)

    # 搬进台账：所有已有 outcome 的轮次，缺一条补一条（分数等标注，不猜）
    moved = 0
    for r in rows:
        if r.get("outcome") is None:
            continue
        kind = ""
        cid = r.get("actual_id") or r.get("chosen")
        for o in (r.get("options") or []):
            if str(o.get("id")) == str(cid):
                kind = str(o.get("kind") or "")
                break
        opts = r.get("options") or []
        d = _find_decision(r.get("ts", ""), want_id=r.get("chosen")) or {}
        sc = (d.get("score") or {})
        bad = [c for c in (sc.get("checks") or []) if c.get("ok") is False]
        analysis = "；".join(x for x in [
            f"候选 {len(opts)} 个",
            f"p_top={r.get('p_top')}" if r.get("p_top") is not None else "",
            f"决策分 {sc.get('score'):.1f}" if sc.get("score") is not None else "",
            f"由头：{str(d.get('why'))[:80]}" if d.get("why") else "",
        ] if x)
        impl = "；".join(x for x in [
            f"{kind} 轮落地",
            "扣分项 " + "、".join(str(c.get("id")) for c in bad[:3]) if bad else "",
            f"目标 {str((d.get('pick') or {}).get('title'))[:40]}" if (d.get("pick") or {}).get("title") else "",
        ] if x) or "无决策文件，只有候选记录"
        result = {1.0: "有可验收产出", 0.5: "半成，产出不完整", 0.0: "空转，只有标题"}.get(
            float(r.get("outcome")), f"outcome={r.get('outcome')}")
        rec = {
            "ts": str(r.get("ts")), "pool": L.pool_for(kind), "kind": kind,
            "title": str(r.get("actual_title") or (opts[0].get("title") if opts else "?")),
            "analysis": analysis, "impl": impl, "result": result,
            "score": None, "by": None, "at": None, "src": "live",
        }
        before = len(L.read())
        L.append(rec)
        if len(L.read()) > before:
            moved += 1
    if not quiet:
        print(f"对账：新回填 {done} 行，未找到决策文件 {miss} 行，"
              f"搬进台账 {moved} 条，总 {len(rows)} 行")
    return {"filled": done, "miss": miss, "moved": moved, "total": len(rows)}


# ---------------- 标注与报告 ----------------

def label(ts: str, score, note: str = "", by: str = "owner") -> dict:
    """给某轮打 0-10 分：0 空转 / 1-5 可能有用 / 6-10 有用。"""
    import jev_ledger as L

    hit = L.mark(ts, score, by=by)
    if hit is not None and note:
        rows = L.read()
        for r in rows:
            if str(r.get("ts")) == str(ts):
                r["note"] = note
        L._write(rows)
    return {"labeled": 1 if hit else 0, "ts": ts, "score": hit.get("score") if hit else None}


def report(write_md: bool = True) -> str:
    import jev_ledger as L

    txt = "# Jev 打分台账  " + _now() + "\n\n" + L.report()
    txt += "\n\n口径：0=空转｜1-5=可能有用｜6-10=有用；人工标注权重 3 倍于自评。\n"
    if write_md:
        try:
            JEV.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(txt, encoding="utf-8")
        except Exception:
            pass
    return txt


def fit_T(quiet: bool = False) -> dict:
    """已退役：温度不再拟合（保留函数名只为兼容旧调用方）。"""
    if not quiet:
        print("fit_T 已退役：现在按 0-10 分回灌，不调温度。")
    return {"fitted": False, "deprecated": True}


if __name__ == "__main__":
    print(reconcile(quiet=False))
    print()
    print(report())
