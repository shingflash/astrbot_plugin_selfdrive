#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""子代理打分任务包：只生成、不调用 API。

为什么不在这个脚本里直接调模型：预算每天 1 rmb，调用时机该由主循环决定。
这个脚本干三件事：
  1. --make   把某一轮拆成干净的任务包（不带历史、不带版本号、不带"这是我写的"）
  2. --list   看还有哪些包没人做
  3. --check  看回填的分数格式对不对、和机械分差多少

子代理约定（对应 cc 的 explore/verification 角色）：
  critic   只找可测反例：这次改动哪一步没被验证、哪个结论没有证据撑
  defender 只交可测证据：哪个文件、哪行、哪个测试能证明它确实做到了
两边都不许说"整体感觉"，必须落到可验证的点名。

结果写 self/score_panel_out.jsonl，一行一条：
  {"target_ts": "...", "roles": {"critic": {...}, "defender": {...}},
   "items": {"p_solves": 4, ...}, "disagreement": 0.2}
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SELF = Path(__file__).resolve().parent
WS = SELF.parent
RUBRIC_FILE = SELF / "score_rubric.json"
ARCHIVE = SELF / "archive.jsonl"
ITER_OUT = SELF / "iter_out"
TASK_DIR = SELF / "score_panel_tasks"
OUT_FILE = SELF / "score_panel_out.jsonl"
WALLET = SELF / "wallet.json"
TS_FMT = "%Y-%m-%d %H:%M:%S"

# 给子代理的硬约束：这是它唯一能看到的世界
SYSTEM_RULES = """你是一个独立的验收员，只做判分，不写代码、不给建议、不延展任务。
规则：
1. 你只看到任务包里的内容，看不到作者是谁、看不到这是第几版、看不到历史。
2. 每条结论必须指向具体的点：文件名、函数名、数值、测试名。禁止写"整体不错""感觉可以"。
3. 找不到证据时写 null，不要猜。
4. 只输出 JSON，不要 markdown 代码块，不要解释。"""

CRITIC_PROMPT = """你是挑错方。任务包如下：

【目标】{goal}
【这一轮声称做的事】{what}
【它自己给的证据】{verify}
【涉及的改动点】{files}

{items}

以 critic 身份回答：每条给 0-5 分（0=完全没有，5=完全做到），并给一条可测反例。
输出：
{{"role":"critic","items":{{"p_solves":0,"p_simplest":0,"p_side_effect":0,"p_evidence":0}},
 "counterexamples":["具体到文件/行/数值的反例"],"missing":["缺哪项证据"]}}"""

DEFENDER_PROMPT = """你是辩护方。任务包如下：

【目标】{goal}
【这一轮声称做的事】{what}
【它自己给的证据】{verify}
【涉及的改动点】{files}

{items}

以 defender 身份回答：每条给 0-5 分，并给一条可测证据（文件/测试名/数值）。
输出：
{{"role":"defender","items":{{"p_solves":0,"p_simplest":0,"p_side_effect":0,"p_evidence":0}},
 "evidence":["具体到文件/行/测试/数值的证据"],"admits":["确实撑不住的点"]}}"""


def now_str() -> str:
    return datetime.now().strftime(TS_FMT)


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_rubric() -> dict:
    return read_json(RUBRIC_FILE, {})


def items_block(rub: dict) -> str:
    lines = []
    for it in (rub.get("panel") or {}).get("items", []):
        lines.append(f"- {it['id']}：{it['desc']}（0-5）")
    return "评分项：\n" + "\n".join(lines)


def latest_decision() -> dict | None:
    if not ITER_OUT.exists():
        return None
    fs = sorted(ITER_OUT.glob("decision_*.json"))
    if not fs:
        return None
    return read_json(fs[-1], None)


def latest_archive_entry() -> dict | None:
    if not ARCHIVE.exists():
        return None
    lines = [l for l in ARCHIVE.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
    for line in reversed(lines):
        try:
            return json.loads(line)
        except Exception:
            continue
    return None


def wallet_gate() -> tuple[bool, str]:
    w = read_json(WALLET, {})
    try:
        bal = float(w.get("balance_rmb", w.get("balance", w.get("rmb", 0))) or 0)
    except Exception:
        bal = 0.0
    if bal <= 0:
        return False, f"工资池 {bal} rmb，先不花钱判分"
    return True, f"工资池 {bal} rmb"


def build_pack(target_ts: str, kind: str, goal: str, what: str, verify: str, files: list) -> dict:
    rub = load_rubric()
    ib = items_block(rub)
    files_s = ", ".join(str(x) for x in (files or [])) or "（未记录）"
    return {
        "target_ts": target_ts,
        "kind": kind,
        "created": now_str(),
        "system": SYSTEM_RULES,
        "prompts": {
            "critic": CRITIC_PROMPT.format(goal=goal, what=what, verify=verify, files=files_s, items=ib),
            "defender": DEFENDER_PROMPT.format(goal=goal, what=what, verify=verify, files=files_s, items=ib),
        },
        "schema": {
            "target_ts": target_ts,
            "roles": {"critic": "{...}", "defender": "{...}"},
            "items": {it["id"]: "0-5 的平均" for it in (rub.get("panel") or {}).get("items", [])},
        },
    }


def packs_for_round() -> list:
    """一轮可能同时有决策和落档改动，各出一个包。"""
    packs = []
    dec = latest_decision()
    if dec:
        g = dec.get("pick") or {}
        packs.append(build_pack(
            dec.get("ts") or now_str(),
            g.get("kind") or "decision",
            g.get("title") or "（本轮未选目标）",
            dec.get("why") or "",
            f"cost={dec.get('cost_rmb')} tier={dec.get('tier')} blocked={dec.get('blocked')}",
            [],
        ))
    e = latest_archive_entry()
    if e:
        packs.append(build_pack(
            e.get("ts") or now_str(),
            e.get("kind") or "archive",
            e.get("goal") or "",
            e.get("what") or "",
            e.get("verify") or "",
            e.get("files") or [],
        ))
    return packs


def make(force: bool = False) -> list:
    ok, why = wallet_gate()
    if not ok and not force:
        print(f"跳过：{why}（要照做加 --force）")
        return []
    TASK_DIR.mkdir(exist_ok=True)
    done = set()
    if OUT_FILE.exists():
        for line in OUT_FILE.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line).get("target_ts"))
            except Exception:
                pass
    made = []
    for p in packs_for_round():
        if p["target_ts"] in done:
            continue
        fp = TASK_DIR / f"pack_{p['target_ts'].replace(':', '').replace(' ', '_')}.json"
        fp.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
        made.append(str(fp))
    print(f"生成 {len(made)} 个任务包" + (f"：{made}" if made else "（都跑过了）"))
    return made


def collect(path: str | None = None) -> int:
    src = Path(path) if path else OUT_FILE
    if not src.exists():
        print("还没有回填文件")
        return 0
    n = 0
    for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        items = d.get("items")
        if not isinstance(items, dict) or not items:
            print(f"跳过 {d.get('target_ts')}：缺 items")
            continue
        bad = [k for k, v in items.items() if not isinstance(v, (int, float)) or not (0 <= v <= 5)]
        if bad:
            print(f"跳过 {d.get('target_ts')}：这些项不在 0-5 → {bad}")
            continue
        n += 1
    print(f"格式合法的打分 {n} 条；judge 会在下次评分时自动并进去")
    return n


def check() -> None:
    if not OUT_FILE.exists():
        print("没有回填")
        return
    rows = []
    for line in OUT_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    if not rows:
        print("没有合法回填")
        return
    for r in rows[-10:]:
        it = r.get("items") or {}
        vals = [v for v in it.values() if isinstance(v, (int, float))]
        avg = round(sum(vals) / len(vals), 2) if vals else None
        spread = round((max(vals) - min(vals)), 2) if len(vals) > 1 else 0
        print(f"{r.get('target_ts')}  均值={avg}/5  两极差={spread}  "
              f"{'⚠ 两极差过大，可能没真看证据' if spread >= 3 else 'ok'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--make", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--collect", nargs="?", const=str(OUT_FILE))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.make:
        make(force=args.force)
    elif args.collect is not None:
        collect(args.collect if args.collect != str(OUT_FILE) else None)
    elif args.check:
        check()
    elif args.list:
        if TASK_DIR.exists():
            for f in sorted(TASK_DIR.glob("pack_*.json")):
                print(f.name)
        else:
            print("没有任务包目录")
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
