# -*- coding: utf-8 -*-
"""维护池的机械判据（2026-11-17 主人重定）。

语义：数据库 = 应然基线（tools/fullscripts_db.py 的内容寻址库），现盘 = 实然。
  丢失的功能越多 → 分越低；实现与库越吻合 → 分越高；
  自己改程序且无误 → 同步进库（build），基线跟着更新。

所以维护池的候选不再手写，由比对结果生成：
  missing  一个脚本一条（按库恢复）
  changed  合并一条（先 audit_selfcheck --probe 全绿再 build 入库）
  new      合并一条（新脚本入库）
  全一致   一条「巡检通过」

每条都带 --survey 可复算的判据，jev 台账里 score 预填审计分（by=self），
人工标注权重 3 倍压过来即可覆盖。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
WS = SELF.parent
TOOLS = WS / "tools"
CST = timezone(timedelta(hours=8))

DRIFT_PENALTY = 0.3   # 漂移未入库的扣项系数：changed 占比 × 10 再乘它
LOSS_WEIGHT = 3.0     # 丢功能比漂移疼 3 倍（主人 2026-11-17：丢得越多分越低）


def now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _h(text: str) -> str:
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()[:8]


def _db():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    import fullscripts_db as db  # noqa: WPS433
    return db


def survey() -> dict:
    """库 vs 现盘：missing/changed/new + 吻合分。库坏了抛异常，由调用方兜。"""
    db = _db()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):   # verify 会打印进度，别污染调用方
        r = db.verify() or {}
    total = int(r.get("total") or 0)
    sv = {
        "ts": now(),
        "total": total,
        "missing": list(r.get("missing") or []),
        "changed": list(r.get("changed") or []),
        "new": list(r.get("new") or []),
        "blob_missing": list(r.get("blob_missing") or []),
        "backdoored": int(r.get("backdoored") or 0),
        "restored_ok": int(r.get("restored_ok") or 0),
    }
    sv["score"] = score_of(sv)
    sv["coverage"] = round(1 - len(sv["missing"]) / total, 4) if total else None
    return sv


def score_of(sv: dict) -> float | None:
    """吻合分（0-10）：丢的越多越低，漂移未入库再扣一点。没基线不给分。"""
    total = int(sv.get("total") or 0)
    if not total:
        return None
    miss = len(sv.get("missing") or [])
    drift = len(sv.get("changed") or [])
    # 丢功能比漂移疼 3 倍：功能没了是损失，漂移只是没同步
    base = 10.0 * (1 - LOSS_WEIGHT * miss / total)
    return round(max(0.0, base - DRIFT_PENALTY * (10.0 * drift / total)), 1)


def _item(iid: str, title: str, priority: int, verify: str, **extra) -> dict:
    it = {"id": iid, "title": title, "kind": "maintain", "cost_rmb": 0,
          "priority": priority, "verify": verify}
    it.update(extra)
    return it


def items(limit_missing: int = 4) -> list[dict]:
    """生成维护池候选。库读不到就返回空表——维护是兜底，不许把整轮搞挂。"""
    try:
        sv = survey()
    except Exception:
        return []
    total = sv["total"]
    miss, drift, new = sv["missing"], sv["changed"], sv["new"]
    out: list[dict] = []
    for path in miss[:limit_missing]:
        out.append(_item(
            "mnt_" + _h(path),
            f"维护：按库恢复丢失脚本 {path}",
            5,
            f"重跑 python self/maintain_audit.py --survey，missing 从 {len(miss)} 降到 {len(miss) - 1}",
            audit=sv["ts"], score_hint=sv["score"]))
    if drift:
        out.append(_item(
            "mnt_drift",
            f"维护：同步 {len(drift)} 个漂移脚本入库（先 audit_selfcheck --probe 全绿再 build）",
            4,
            f"重跑 --survey 看 changed 是否从 {len(drift)} 降到 0",
            audit=sv["ts"], score_hint=sv["score"],
            changed=[str(x.get("path") if isinstance(x, dict) else x) for x in drift[:12]]))
    if new:
        out.append(_item(
            "mnt_new",
            f"维护：新脚本 {len(new)} 个入库（与库对齐，它们还没基线版本）",
            3,
            f"重跑 --survey 看 new 是否从 {len(new)} 降到 0",
            audit=sv["ts"], score_hint=sv["score"],
            new=[str(x) for x in new[:12]]))
    if not out:
        out.append(_item(
            "mnt_clean",
            f"维护：库与现盘一致（{total} 个脚本），巡检通过并记录",
            1,
            f"重跑 --survey 确认 missing/changed/new 都还是 0",
            audit=sv["ts"], score_hint=sv["score"]))
    return out


def _probe_ok() -> tuple[bool, str]:
    """同步前置：自检探针全绿（编译、入口、目录、记忆）。"""
    try:
        r = subprocess.run(
            [sys.executable, str(TOOLS / "audit_selfcheck.py"), "--probe", "--json"],
            cwd=str(WS), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600)
    except Exception as e:
        return False, f"探针跑不起来：{type(e).__name__}: {e}"
    if r.returncode != 0:
        return False, f"探针 rc={r.returncode}: {(r.stdout or r.stderr or '')[-200:]}"
    try:
        js = json.loads((r.stdout or "").strip())
    except Exception:
        return False, "探针输出不是 JSON"
    if js.get("dirs_missing"):
        return False, f"缺目录：{js['dirs_missing'][:3]}"
    sc = js.get("scripts") or {}
    if sc.get("backdoored"):
        return False, f"有篡改/后门标记 {sc['backdoored']} 个"
    return True, "探针全绿"


def restore_missing(dry: bool = False, limit: int = 8) -> dict:
    """把库里有的丢失脚本补回现盘。功能回来 → missing 降 → 分自然回。

    只补现盘已经没有的文件（missing 的定义），绝不覆盖现存文件；
    补回的 .py 当场 py_compile 核一遍，编不过算没补成。
    """
    sv = survey()
    miss = sv["missing"]
    if not miss:
        return {"ok": True, "nothing": True, "score": sv["score"]}
    db = _db()
    rows = []
    for it in miss[:limit]:
        rel = str(it.get("path") if isinstance(it, dict) else it)
        dest = WS / rel.replace("/", "\\")
        if dest.exists():
            rows.append({"path": rel, "skip": "现盘已有，不动"})
            continue
        if dry:
            rows.append({"path": rel, "dry": True})
            continue
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = False
            try:
                ok = bool(db.restore(rel))
            except Exception as e:
                rows.append({"path": rel, "error": f"{type(e).__name__}: {e}"[:120]})
                continue
        row = {"path": rel, "restored": ok}
        if ok and str(rel).endswith(".py"):
            try:
                r = subprocess.run([sys.executable, "-m", "py_compile", str(dest)],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=120)
                row["compile"] = "ok" if r.returncode == 0 else (r.stderr or "")[-120:]
            except Exception as e:
                row["compile"] = f"{type(e).__name__}"
        rows.append(row)
    after = sv if dry else survey()
    return {"ok": True, "restored": rows,
            "score_before": sv["score"], "score_after": after["score"],
            "missing_before": len(miss), "missing_left": len(after["missing"])}


def sync(dry: bool = False) -> dict:
    """把现盘同步入库（build）。先探针确认改动无误，过不了就拒绝。"""
    ok, why = _probe_ok()
    if not ok:
        return {"ok": False, "reason": why}
    if dry:
        return {"ok": True, "dry": True, "reason": why}
    db = _db()
    buf = io.StringIO()
    before = score_of(survey())
    with contextlib.redirect_stdout(buf):
        db.build()
    after = score_of(survey())
    return {"ok": True, "before": before, "after": after,
            "log": buf.getvalue().strip().splitlines()[-3:]}


def record_round(pick: dict, result: str = "", ts: str | None = None,
                 path: Path | None = None) -> dict | None:
    """维护轮结账时把审计分写进 jev 台账（自评，人工可覆盖）。

    优先给台账里已存在的同题待标行合并分（append 的 _key 合并会锁住 owner 标注），
    没有才新写一行 —— 免得 reconcile 之后再冒出两条同轮记录。
    """
    try:
        import jev_ledger as L
        sv = survey()
        rows = L.read(path)
    except Exception:
        return None
    gid = str(pick.get("id") or "")
    # 同题就近合并：认 id 或标题前 24 字；人工标过就整个跳过，改动人工结论
    target = None
    for r in reversed(rows):
        if r.get("pool") != "maintain":
            continue
        if str(r.get("id") or "") == gid or str(r.get("title"))[:24] == str(pick.get("title"))[:24]:
            target = r
            break
    if target is not None and target.get("by") == "owner" and target.get("score") is not None:
        return {"skip": f"人工已标 {target.get('score')} 分，自评不动", "ts": target.get("ts")}
    rec = {
        "ts": (target or {}).get("ts") or ts or now(),
        "pool": "maintain",
        "kind": "maintain",
        "id": gid,
        "title": (target or {}).get("title") or str(pick.get("title") or "维护轮"),
        "analysis": (f"库基线 {sv['total']} 个脚本；missing {len(sv['missing'])}、"
                     f"changed {len(sv['changed'])}、new {len(sv['new'])}；"
                     f"吻合分 {sv['score']}（丢得越少越高）"),
        "impl": f"候选 {gid}；{pick.get('verify') or '按 --survey 数字核对'}",
        "result": result or "审计轮，见 --survey 前后差异",
        "score": sv["score"],
        "by": "self",
        "src": "maintain_audit",
    }
    try:
        return L.append(rec, path)
    except Exception:
        return None


def report() -> str:
    sv = survey()
    lines = [f"维护审计 {sv['ts']}：库 {sv['total']} 个脚本，吻合分 {sv['score']}（"
             f"missing {len(sv['missing'])} / changed {len(sv['changed'])} / "
             f"new {len(sv['new'])} / blob 缺 {len(sv['blob_missing'])}）"]
    if sv["missing"]:
        lines.append("missing：")
        lines += [f"  - {p}" for p in sv["missing"][:10]]
    if sv["changed"]:
        lines.append("changed（改了未入库）：")
        lines += [f"  - {x.get('path') if isinstance(x, dict) else x}" for x in sv["changed"][:10]]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--survey", action="store_true", help="库 vs 现盘比对（只读）")
    ap.add_argument("--items", action="store_true", help="输出维护池候选 JSON")
    ap.add_argument("--sync", action="store_true", help="探针全绿后把现盘同步入库")
    ap.add_argument("--restore-missing", action="store_true",
                    help="把库里有的丢失脚本补回现盘（missing），补完重算分")
    ap.add_argument("--dry", action="store_true", help="配合 --sync/--restore-missing：只看不写")
    a = ap.parse_args()
    if a.restore_missing:
        print(json.dumps(restore_missing(dry=a.dry), ensure_ascii=False, indent=1))
    elif a.sync:
        print(json.dumps(sync(dry=a.dry), ensure_ascii=False, indent=1))
    elif a.items:
        print(json.dumps(items(), ensure_ascii=False, indent=1))
    elif a.survey:
        print(json.dumps(survey(), ensure_ascii=False, indent=1)[:3000])
    else:
        print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
