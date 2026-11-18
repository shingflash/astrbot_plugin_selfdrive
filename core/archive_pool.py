# -*- coding: utf-8 -*-
"""归档池料源（2026-11-17，第八池唯一的生成器）。

主人口径：料源每次运行至少补一条；补得越少说明在空转。所以这脚本不做
「没有货也造一条」的事 —— 当天没有新轮次就返回空，让 refill 那边如实
记成「+0」，这个 0 才是空转的证据。

一条 = 一天（材料口径：归档是给回退用的，不是日记）：
  · 把当天所有轮次的 pick / 结论 / jev 分数压成一条可核对的归档任务
  · 评分规则：只凭归档材料回退重建本轮动过的程序，得分 = 跑通的功能数/应有功能数；
    丢一项功能扣 1/b，材料缺到无法回退记 0 —— 回退做得越全分越高
  · 精炼：单程序在 changes.md 里超过 10 条 → 先压成一条「当前有效事实 + 回退要点」
    （旧条目不删，标 SUPERSEDED），压完再谈新归档
  · id 按天唯一（arc_20261117，同一天第二次归档是 arc_20261117_r2）
  · 只读 iter_out/decision_*.json / jev/rounds.jsonl / changes.md，不联网不烧钱

用法:
  python archive_pool.py                    # 看今天会生成什么
  python archive_pool.py --day 2026-11-16   # 指定某天
  python archive_pool.py --json             # 直接输出候选 JSON
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
ITER_OUT = SELF / "iter_out"
ROUNDS = SELF / "jev" / "rounds.jsonl"
CHANGES = SELF / "changes.md"
POLISH_AT = 10          # 单程序归档条目上限，超过就先精炼（主人 2026-11-17 定的口径）
CST = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(CST)


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def decisions(day: str) -> list[dict]:
    """当天所有轮次的 decision（按文件 ts 字段过滤，不靠文件名）。"""
    out = []
    if not ITER_OUT.exists():
        return out
    for f in ITER_OUT.glob("decision_*.json"):
        d = _read_json(f)
        if isinstance(d, dict) and str(d.get("ts") or "").startswith(day):
            d["_file"] = f.name
            out.append(d)
    out.sort(key=lambda d: str(d.get("ts") or ""))
    return out


def rounds(day: str) -> list[dict]:
    """当天 jev 抽样台账（给归档条目带上「被标几分」的证据）。"""
    out = []
    if not ROUNDS.exists():
        return out
    for line in ROUNDS.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if str(r.get("ts") or "").startswith(day):
            out.append(r)
    return out


def _archive_marks(decs: list[dict]) -> list[str]:
    """当天已经跑过的归档轮次时间戳（增量归档的切点）。"""
    return [str(d.get("ts") or "") for d in decs
            if (d.get("pick") or {}).get("kind") == "archive" and not d.get("blocked")]


def _archived_targets(decs: list[dict]) -> set:
    """当天已经精炼过的程序（同一天不重复给同一条精炼）。"""
    return {str((d.get("pick") or {}).get("target"))
            for d in decs
            if (d.get("pick") or {}).get("kind") == "archive" and not d.get("blocked")
            and (d.get("pick") or {}).get("target")}


def _hot_program(limit: int = POLISH_AT):
    """changes.md 里改动条目最多的程序；超过 limit 条返回 (程序, 条数)。

    按「条目」统计，不按文件名出现次数：同一条改动里同一文件出现多次只算一条。
    """
    if not CHANGES.exists():
        return None
    prog_re = re.compile(r"([A-Za-z0-9_\-]+\.py)")
    counts: dict[str, int] = {}
    seen: set = set()
    for line in CHANGES.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            seen = set()
        for hit in prog_re.findall(line):
            if hit not in seen:
                counts[hit] = counts.get(hit, 0) + 1
                seen.add(hit)
    if not counts:
        return None
    prog, n = max(counts.items(), key=lambda kv: kv[1])
    return (prog, n) if n > limit else None


def build(day: str | None = None) -> dict | None:
    """生成增量归档候选：只覆盖「上次归档之后」的新轮次。

    没新轮次就返回 None —— 补 0 条是空转证据，不许造一条固定文案凑数。
    """
    day = day or now().strftime("%Y-%m-%d")
    decs_all = decisions(day)
    if not decs_all:
        return None
    marks = _archive_marks(decs_all)
    decs = ([d for d in decs_all if str(d.get("ts") or "") > marks[-1]]
            if marks else list(decs_all))
    if not decs:
        return None
    rs = rounds(day)
    rows = []
    n_old = len(marks)
    for d in decs:
        pk = d.get("pick") or {}
        rows.append({
            "ts": d.get("ts"),
            "pool": d.get("pool_now") or (pk.get("kind") or ""),
            "id": pk.get("id"),
            "title": (pk.get("title") or "")[:80],
            "cost_rmb": d.get("cost_rmb"),
            "blocked": d.get("blocked") or None,
        })
    scored = [r.get("outcome") for r in rs if r.get("outcome") is not None]
    zero = sum(1 for x in scored if not x)
    pools = sorted({str(r["pool"]) for r in rows if r.get("pool")})

    # 精炼优先（主人 2026-11-17）：单程序在 changes.md 里超过 10 条 → 先压成一条再谈新归档
    hot = _hot_program()
    done_targets = _archived_targets(decs_all)
    if hot and hot[0] not in done_targets:
        prog, n = hot
        return {
            "id": f"arc_polish_{Path(prog).stem}_{day.replace('-', '')}",
            "title": f"精炼 {prog}：归档里已有 {n} 条（>{POLISH_AT}）→ 压成当前有效事实 + 回退要点",
            "kind": "archive",
            "subtype": "polish",
            "target": prog,
            "entries": n,
            "cost_rmb": 0,
            "priority": 4,
            "tier": "t2_write_low",
            "archive_day": day,
            "rounds": rows,
            "jev": {"samples": len(rs), "scored": len(scored), "zero": zero},
            "score_rule": "回退覆盖率：只凭压缩后那一条重建程序，跑通的功能数/应有功能数",
            "verify": (
                f"changes.md 里 {prog} 已经有 {n} 条（>{POLISH_AT}）：先把它们压成一条『当前有效事实』——"
                "保留 现版本 / 必须留的函数与开关 / 回退要点（备份名 + 恢复步骤）/ 踩过的坑（失败原因不删）；"
                "旧条目不删，在 changes.md 尾部标 SUPERSEDED 并写日期；"
                "压完验证：只凭压缩后这一条回退，跑通的功能数/应有功能数 = 本轮得分，丢功能的写明丢哪项。"
            ),
        }

    return {
        "id": "arc_" + day.replace("-", "") + (f"_r{n_old + 1}" if n_old else ""),
        "title": f"归档 {day} 第{n_old + 1}次：{len(decs)} 轮（池 {'/'.join(pools) or '无'}）→ 回退验证",
        "kind": "archive",
        "cost_rmb": 0,
        "priority": 3,
        "tier": "t2_write_low",
        "archive_day": day,
        "rounds": rows,
        "jev": {"samples": len(rs), "scored": len(scored), "zero": zero},
        "score_rule": "回退覆盖率：只凭归档材料重建本轮动过的程序，跑通的功能数/应有功能数；丢一项扣 1/b，材料缺到无法回退记 0",
        "verify": (
            f"归档 {day} 的 {len(decs)} 轮，产出是回退材料不是日记：① self/log.md 每轮一行（池/题目/产出的文件或结论）"
            "② self/changes.md 每轮一条（动了哪个文件哪个函数、备份名、失败过什么）；"
            "然后回退验证：只凭这两份文件 + backup/ 实测重建本轮动过的程序，"
            "逐项核对功能清单（入口/子命令/开关/输出字段），"
            "记『覆盖 a/b，丢了哪些项』写进 changes.md 归档段 —— 覆盖率 a/b 就是本轮 outcome。"
            + (f"另附空转复盘：当天 {zero} 轮被标 0 分，逐条写为什么。" if zero else "")
        ),
    }


def items(day: str | None = None) -> list:
    it = build(day)
    return [it] if it else []


def show(day: str | None = None) -> int:
    d = day or now().strftime("%Y-%m-%d")
    it = build(d)
    if not it:
        print(f"[archive] {d} 无新轮次或当天已归档 → 补 0 条（空转信号，不硬造）")
        return 0
    print(f"[archive] {it['id']}  {it['title']}")
    if it.get("subtype") == "polish":
        print(f"  精炼目标 {it.get('target')}（{it.get('entries')} 条）")
    print(f"  计分 {it.get('score_rule')}")
    print(f"  轮次 {len(it['rounds'])}，jev 样本 {it['jev']['samples']}（0 分 {it['jev']['zero']}）")
    for r in it["rounds"]:
        print(f"  · {r['ts']}  {r['pool']:<10}  {r['title']}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    day = None
    if "--day" in args:
        i = args.index("--day")
        if i + 1 < len(args):
            day = args[i + 1]
    if "--json" in args:
        it = build(day)
        print(json.dumps(it, ensure_ascii=False, indent=2) if it else "null")
        return 0
    return show(day)


if __name__ == "__main__":
    sys.exit(main())
