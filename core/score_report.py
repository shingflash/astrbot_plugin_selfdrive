#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 score_history.jsonl 画成能看的报告，落 self/score_report.md。

不复述每一条判据细节，只回答四个问题：
  1. 最近在涨还是在跌
  2. 哪类动作（好奇/优化/学习/维持）分数低
  3. 最容易犯的哪条硬伤
  4. 子代理面板覆盖率多少（低了说明只有我在自评，结论要打折）
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
HISTORY = SELF / "score_history.jsonl"
OUT = SELF / "score_report.md"
RUBRIC = SELF / "score_rubric.json"

REMEDY = {
    "d_pick": "空转：查 goals.json 的钱包门槛与 pick() 的 money_block",
    "d_gate": "闸门已停却仍 pick：查 main() 里 blocked 的赋值顺序",
    "d_streak": "原地转圈：加 STALE_STEP / kind_streak 惩罚",
    "d_mix": "动作类型单一：查 VALUE_BONUS 是否把别的 kind 压死",
    "d_cost_worth": "花了钱没对应强度：降到 t2 或换零成本目标",
    "a_backup": "改之前没备份：写操作先 Copy-Item 一份 .bak",
    "a_files_real": "声称改的文件没被动过：核对 files 与 mtime",
    "a_verify_proof": "verify 全是形容词：补 X/Y、rc=0、测试名或前后数值",
    "a_self_praise": "verify 有不可证伪的自夸：换数字",
    "a_no_self_praise": "verify 有不可证伪的自夸：换数字",
    "a_used_by": "上轮改动没被用上：补 used_by 或承认白改",
    "a_result_consistent": "pass 但证据不足：降成 partial",
}


def load_rows(limit: int = 200) -> list:
    if not HISTORY.exists():
        return []
    rows = []
    for line in HISTORY.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows[-limit:]


def spark(vals: list) -> str:
    """纯 ASCII 曲线，比表格省眼。"""
    if not vals:
        return "（无数据）"
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    return "".join(blocks[min(7, int((v - lo) / span * 7.99))] for v in vals)


def build(rows: list) -> str:
    if not rows:
        return "# 自迭代评分\n\n还没有历史，先跑 `python score_judge.py --decision latest`。\n"

    vals = [float(r.get("score") or 0) for r in rows]
    recent = vals[-12:]
    trend = "—"
    if len(vals) >= 6:
        a = sum(vals[-6:-3]) / 3
        b = sum(vals[-3:]) / 3
        trend = f"{'↑' if b > a else ('↓' if b < a else '→')} {a:.1f} → {b:.1f}"

    by_kind, by_layer, flags = {}, {}, {}
    panel_n = 0
    for r in rows:
        s = float(r.get("score") or 0)
        by_kind.setdefault(r.get("kind") or "-", []).append(s)
        by_layer.setdefault(r.get("layer") or "-", []).append(s)
        for f in r.get("flags") or []:
            flags[f] = flags.get(f, 0) + 1
        if (r.get("panel") or {}).get("score") is not None:
            panel_n += 1

    avg = lambda xs: sum(xs) / len(xs)
    lines = [
        f"# 自迭代评分报告（{datetime.now().strftime('%Y-%m-%d %H:%M')}）",
        "",
        f"- 样本：{len(rows)} 轮　最新：{vals[-1]:.1f}　历史均值：{avg(vals):.1f}　趋势：{trend}",
        f"- 最近 12 轮：`{spark(recent)}`",
        f"- 子代理面板覆盖：{panel_n}/{len(rows)}"
        + ("（偏低 → 现在的分主要是机械层判的，只代表没硬伤，不代表做得好）" if panel_n / len(rows) < 0.3 else ""),
        "",
        "## 按层级",
    ]
    for k, v in sorted(by_layer.items(), key=lambda x: -len(x[1])):
        lines.append(f"- {k}：n={len(v)} 均值={avg(v):.1f} 最低={min(v):.1f}")
    lines += ["", "## 按动作类型"]
    for k, v in sorted(by_kind.items(), key=lambda x: avg(x[1])):
        lines.append(f"- {k}：n={len(v)} 均值={avg(v):.1f}")
    lines += ["", "## 最常犯的硬伤"]
    if not flags:
        lines.append("- 无")
    for f, n in sorted(flags.items(), key=lambda x: -x[1])[:6]:
        lines.append(f"- {f} ×{n}  → {REMEDY.get(f, '')}")
    lines += ["", "## 判据入口", "- `self/score_rubric.json`（改判据只改这里）",
              "- `python self/score_judge.py --summary`", "- `python self/score_panel.py --make`"]
    return "\n".join(lines) + "\n"


def main() -> int:
    rows = load_rows()
    md = build(rows)
    OUT.write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
