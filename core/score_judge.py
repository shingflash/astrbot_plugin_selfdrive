#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机械判官：自迭代每一轮的验收打分。

设计（2026-11-13 主人定）：
1. 不单独开"优化轮"，每一轮都参与（curiosity / inspiration / explore /
   learn / optimize / meta / maintain / practice 一视同仁）。
2. 能机械算的绝不给模型算 —— 这个文件零 token、零网络。
3. 模型只碰主观项，走 score_panel.py 生成任务包，结果落 score_panel_out.jsonl。
4. 主人手改过的版本当金标，放 self/score_gold/，权重最高。
5. 任何异常只记录、不抛，绝不打断 iter_cycle 主流程。

用法：
    python score_judge.py --decision latest          # 给最新一轮决策打分
    python score_judge.py --decision <path.json>
    python score_judge.py --archive                   # 补评 archive 里未评的条目
    python score_judge.py --archive --dry             # 只看不写
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
WS = SELF.parent
RUBRIC_FILE = SELF / "score_rubric.json"
HISTORY_FILE = SELF / "score_history.jsonl"
SCORED_FILE = SELF / "score_scored.json"
PANEL_OUT = SELF / "score_panel_out.jsonl"
ARCHIVE = SELF / "archive.jsonl"
ITER_OUT = SELF / "iter_out"
GOLD_DIR = SELF / "score_gold"

TS_FMT = "%Y-%m-%d %H:%M:%S"

# 判据失败时给一句可执行的补救，别只报错
REMEDY = {
    "d_pick": "本轮没选出目标：检查 goals.json 的 cost_rmb 是否全都超过钱包余额，或 pick() 的 money_block 是否误判",
    "d_cost_cap": "成本越界：看 decision.cost_rmb 与 quota_state()，别让单轮成本破 0.2",
    "d_gate": "闸门已 stop/halt 却仍 pick：检查 main() 里 blocked 分支的赋值顺序",
    "d_streak": "连续原地转圈：给 pick() 的 STALE_STEP / kind_streak 惩罚加权",
    "d_mix": "动作类型太单一：检查 VALUE_BONUS 与 kind_streak 是否把别的 kind 压死了",
    "d_curiosity_fresh": "好奇候选是陈的或吃过的：跑 curiosity.py build 刷新，并查 consumed_* 的扫描文件数",
    "d_cost_worth": "花了钱却没对应产出强度：把 cost_rmb 降到 t2 档，或换成零成本目标",
    "a_backup": "改之前没备份：任何 self/tools/skills 下的写操作先 Copy-Item 一份 .bak",
    "a_files_real": "声称改的文件没真被动过（或路径写错）：核对 files 字段与 mtime",
    "a_verify_proof": "verify 全是形容词：补一条可复现证据（X/Y、rc=0、pytest 名、具体数值前后）",
    "a_no_self_praise": "verify 里有不可证伪的自夸词：换成数字或删掉",
    "a_used_by": "上一轮 meta/optimize 的改动没被下一轮用上：要么补 used_by，要么承认白改",
    "a_result_consistent": "result=pass 但证据不足：降成 partial，别直接写 pass",
}

PROOF_PATTERNS = [
    r"\d+\s*/\s*\d+",                      # 13/13
    r"rc\s*=\s*\d+",                       # rc=0
    r"\bPASS\b",
    r"\bpass\b",
    r"test_[A-Za-z0-9_]+",
    r"pytest|py_compile|npm\s+test|python\s+-m",
    r"\d+\.\d+\s*(?:→|->|=>)\s*\d+\.\d+",  # 59.37→67.27
]
PROOF_RE = re.compile("|".join(PROOF_PATTERNS), re.I)


def now() -> datetime:
    return datetime.now()


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_rubric() -> dict:
    r = read_json(RUBRIC_FILE, None)
    if not r:
        r = {"base": 60, "score_range": [0, 100], "layers": {}, "panel": {},
             "forbidden_self_praise": [], "kind_weights": {}}
    return r


def _parse_ts(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19], TS_FMT)
    except Exception:
        return None


def _recent_kinds(n: int = 15) -> list:
    """最近 n 轮的「环节」序列：动作太单一要扣分，但样本不足时不判。

    好奇心轮按 curiosity_cat 细分（热点话题/前沿科技/灵感/实践…）。
    原因：好奇轮零成本 +10 分，长期压死 explore/maintain/archive，kind 只剩
    curiosity 一种，d_mix 要求 3 种 → 结构性不可满足的假红灯（2026-11-16 挖出，
    11-17 主人下令维修）。细分后验算由 2 种变 6 种。
    """
    if not ITER_OUT.exists():
        return []
    out = []
    for f in sorted(ITER_OUT.glob("decision_*.json"))[-n:]:
        d = read_json(f, None)
        if not isinstance(d, dict):
            continue
        pick = d.get("pick") or {}
        k = pick.get("kind") or d.get("blocked")
        if k:
            k = str(k)
            if k == "curiosity":
                cat = str(pick.get("curiosity_cat") or "").strip()
                if cat:
                    k = f"curiosity:{cat}"
            out.append(k)
    return out


def _check(cid: str, ok, detail: str, v=1.0) -> dict:
    """ok=True -> v=1，ok=False -> v=0，ok=None -> 中性 0.5"""
    if ok is None:
        val = 0.5
    else:
        val = float(v) if ok else 0.0
    return {"id": cid, "ok": ok, "v": val, "detail": detail}


# ---------------------------------------------------------------- decision 层
def check_decision(dec: dict, rub: dict) -> list:
    g = dec.get("pick") or {}
    checks = []
    blocked = dec.get("blocked")
    lv = dec.get("level") or ""
    gate = dec.get("gate") or ""
    cur = dec.get("curiosity") or {}
    cost = float(dec.get("cost_rmb") or 0)
    qs = dec.get("quota_state")

    # d_pick
    if g:
        checks.append(_check("d_pick", True, f"选了「{g.get('title', '')[:40]}」"))
    elif blocked in ("halt", "gate"):
        checks.append(_check("d_pick", None, f"被 {blocked} 挡住，中性"))
    else:
        checks.append(_check("d_pick", False, f"空转（blocked={blocked or '无'}）"))

    # d_cost_cap
    ok_cap = cost <= 0.2 + 1e-9 and qs != "empty"
    checks.append(_check("d_cost_cap", ok_cap, f"cost={cost} quota={qs}"))

    # d_gate
    if lv == "halt" or gate == "stop":
        checks.append(_check("d_gate", not g, f"level={lv} gate={gate} pick={'有' if g else '无'}"))
    else:
        checks.append(_check("d_gate", True, f"level={lv} gate={gate} 未拦"))

    # d_streak（pick_streak 是 {gid: 次数}，不是整数）
    ps_raw = dec.get("pick_streak")
    if isinstance(ps_raw, dict) and ps_raw:
        ps = max(int(v) for v in ps_raw.values())
        ps_desc = ",".join(f"{k}x{v}" for k, v in list(ps_raw.items())[:2])
    else:
        ps = 0
        ps_desc = "-"
    ks = dec.get("kind_streak") or {}
    kn = int(ks.get("n") or 0)
    checks.append(_check("d_streak", ps <= 3 and kn <= 4,
                         f"pick_streak={ps_desc}({ps}) kind_streak={ks.get('kind')}x{kn}"))

    # d_mix（从最近决策文件里取 kind，比 decision 里的摘要准）
    kinds = _recent_kinds(15)
    if len(kinds) >= 5:
        checks.append(_check("d_mix", len(set(kinds)) >= 3, f"最近{len(kinds)}轮种类={sorted(set(kinds))}"))
    else:
        checks.append(_check("d_mix", None, f"样本不足（{len(kinds)} 轮），中性"))

    # d_curiosity_fresh
    pool = cur.get("pool")
    if cur.get("ok") is False and pool == 0 and int(cur.get("pool_total") or 0) > 0:
        checks.append(_check("d_curiosity_fresh", False, f"池子 {pool}/{cur.get('pool_total')} 全被吃过"))
    else:
        checks.append(_check("d_curiosity_fresh", True, f"池 {pool if pool is not None else '-'}"))

    # d_cost_worth
    tier = dec.get("tier") or ""
    if cost <= 0:
        checks.append(_check("d_cost_worth", True, "零成本轮"))
    else:
        checks.append(_check("d_cost_worth", tier in ("t2_write_low", "t3_self_code"),
                             f"cost={cost} tier={tier}"))
    return checks


# ---------------------------------------------------------------- archive 层
def _file_touched(paths: list, ts: datetime | None, strict: bool = True) -> tuple[bool, str]:
    if not paths:
        return None, "没写 files 字段"
    hits, miss = 0, []
    for rel in paths:
        p = WS / str(rel).replace("\\", "/")
        if not p.exists():
            miss.append(f"{rel}(不存在)")
            continue
        if ts is not None and strict:
            mt = datetime.fromtimestamp(p.stat().st_mtime)
            if abs((mt - ts).total_seconds()) > 3 * 3600:
                miss.append(f"{rel}(mtime={mt.strftime('%m-%d %H:%M')} 离 {ts.strftime('%m-%d %H:%M')} 太远)")
                continue
        hits += 1
    if hits == 0:
        return False, "; ".join(miss)[:180]
    return True, f"{hits}/{len(paths)} 命中" + ("; " + "; ".join(miss) if miss else "")


def check_archive(entry: dict, rub: dict) -> list:
    checks = []
    ts = _parse_ts(entry.get("ts"))
    kind = entry.get("kind") or ""
    verify = str(entry.get("verify") or "")
    result = str(entry.get("result") or "")

    # a_backup
    bk = str(entry.get("backup") or "")
    if not bk:
        checks.append(_check("a_backup", False, "没写 backup"))
    else:
        exists = (WS / bk.replace("\\", "/")).exists()
        checks.append(_check("a_backup", exists, f"{bk}{'' if exists else ' 不存在'}"))

    # a_files_real（24h 内的条目严格比 mtime；老条目后续可能被再改，只看存不存在）
    age_h = ((now() - ts).total_seconds() / 3600) if ts else 0
    ok_files, det_files = _file_touched(entry.get("files") or [], ts, strict=(age_h <= 24))
    checks.append(_check("a_files_real", ok_files, det_files))

    # a_verify_proof
    m = PROOF_RE.search(verify)
    checks.append(_check("a_verify_proof", bool(m), (m.group(0) if m else "无 X/Y、rc=、PASS、测试名、数值变化")))

    # a_no_self_praise
    bad = [w for w in (rub.get("forbidden_self_praise") or []) if w in verify]
    checks.append(_check("a_no_self_praise", not bad, ("命中: " + ",".join(bad)) if bad else "无自夸词"))

    # a_used_by
    if kind in ("meta", "optimize"):
        ub = entry.get("used_by")
        checks.append(_check("a_used_by", bool(ub), f"used_by={ub or '空'}"))
    else:
        checks.append(_check("a_used_by", None, f"kind={kind} 不要求"))

    # a_result_consistent
    if result == "pass" and not m:
        checks.append(_check("a_result_consistent", False, "写了 pass 但没有可复现证据"))
    else:
        checks.append(_check("a_result_consistent", True, f"result={result or '-'}"))
    return checks


# ---------------------------------------------------------------- 合成
def _gold_bonus(kind: str) -> dict | None:
    """金标：主人手改过的同类改动，给一次加分参考（只读，不自动改判）。"""
    if not GOLD_DIR.exists():
        return None
    files = sorted(GOLD_DIR.glob("gold_*.json"))
    if not files:
        return None
    for f in reversed(files):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("kind") == kind and isinstance(d.get("score"), (int, float)):
            return {"file": f.name, "score": d["score"], "note": d.get("note", "")}
    return None


def _panel_lookup(target_ts: str) -> dict | None:
    if not PANEL_OUT.exists():
        return None
    best = None
    try:
        for line in PANEL_OUT.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if str(d.get("target_ts")) == str(target_ts):
                best = d
    except Exception:
        return None
    return best


def compose(layer: str, target_ts: str, checks: list, rub: dict, kind: str = "") -> dict:
    weights = {}
    for it in (rub.get("layers") or {}).get(layer, []):
        weights[it["id"]] = float(it.get("w") or 1)
    tot = sum(weights.get(c["id"], 0) for c in checks) or 1.0
    got = sum(weights.get(c["id"], 0) * c["v"] for c in checks)
    score = round(100.0 * got / tot, 1)
    # 机械层永远证明不了"做得好"，只证明"没硬伤"：决策层封顶 92，防止满分幻觉
    if layer == "decision":
        score = min(score, 92.0)
    flags = [c["id"] for c in checks if c["ok"] is False]
    neutrals = [c["id"] for c in checks if c["ok"] is None]

    panel = _panel_lookup(target_ts)
    out = {
        "ts": now().strftime(TS_FMT),
        "layer": layer,
        "target_ts": target_ts,
        "kind": kind,
        "score": score,
        "checks": checks,
        "flags": flags,
        "neutrals": neutrals,
        "remedies": [REMEDY.get(f, "") for f in flags if REMEDY.get(f)],
        "panel": None,
    }

    # 机械层先判，模型层只微调（最多 ±10），且分歧单独记，不吞掉
    if panel and isinstance(panel.get("items"), (dict, list)):
        pscore, pdis = _panel_score(panel, rub)
        if pscore is not None:
            delta = max(-10.0, min(10.0, pscore - 50.0) / 5.0)
            out["panel"] = {"score": pscore, "disagreement": pdis,
                            "delta_applied": round(delta, 1),
                            "roles": panel.get("roles")}
            out["score"] = round(max(0.0, min(100.0, score + delta)), 1)
    else:
        out["panel"] = {"status": "pending", "note": "没跑子代理；score_panel.py 生成任务包后补"}

    gold = _gold_bonus(kind)
    if gold:
        out["gold"] = gold
        out["score_gap_vs_gold"] = round(out["score"] - float(gold["score"]), 1)
    return out


def _panel_score(panel: dict, rub: dict) -> tuple[float | None, float | None]:
    items = panel.get("items")
    agg = {}
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            k, v = it.get("id"), it.get("score")
            if isinstance(v, (int, float)):
                agg.setdefault(k, []).append(float(v) * 20.0)  # 0-5 -> 0-100
    elif isinstance(items, dict):
        for k, v in items.items():
            if isinstance(v, (int, float)):
                agg.setdefault(k, []).append(float(v) * 20.0)
    if not agg:
        return None, None
    means = {k: sum(v) / len(v) for k, v in agg.items()}
    pscore = round(sum(means.values()) / len(means), 1)
    spread = max(means.values()) - min(means.values())
    return pscore, round(spread / 100.0, 3)


# ---------------------------------------------------------------- 写盘
def write_history(layer: str, result: dict):
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        slim = dict(result)
        slim["checks"] = [{"id": c["id"], "ok": c["ok"]} for c in result.get("checks", [])]
        f.write(json.dumps(slim, ensure_ascii=False) + "\n")


def score_decision(dec: dict, rub: dict | None = None) -> dict:
    rub = rub or load_rubric()
    checks = check_decision(dec, rub)
    kind = ((dec.get("pick") or {}).get("kind") or "")
    res = compose("decision", dec.get("ts", ""), checks, rub, kind)
    return res


def latest_decision_file() -> Path | None:
    if not ITER_OUT.exists():
        return None
    fs = sorted(ITER_OUT.glob("decision_*.json"))
    return fs[-1] if fs else None


def score_latest_decision() -> dict | None:
    p = latest_decision_file()
    if not p:
        return None
    try:
        dec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    res = score_decision(dec)
    dec["score"] = res
    try:
        p.write_text(json.dumps(dec, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    write_history("decision", res)
    return res


def _load_scored() -> set:
    d = read_json(SCORED_FILE, {"ts": []})
    return set(d.get("ts") or [])


def _save_scored(s: set):
    SCORED_FILE.write_text(json.dumps({"ts": sorted(s)}, ensure_ascii=False, indent=2), encoding="utf-8")


def score_archive(commit: bool = True, limit: int = 20) -> list:
    if not ARCHIVE.exists():
        return []
    rub = load_rubric()
    done = _load_scored()
    lines = ARCHIVE.read_text(encoding="utf-8", errors="ignore").splitlines()
    out, changed, scored = [], [], set(done)

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            out.append(line)
            continue
        ts = str(e.get("ts") or "")
        if ts in done or not ts:
            out.append(json.dumps(e, ensure_ascii=False))
            continue
        if len([1 for x in out if x and x.startswith("{")]) >= 0 and len(changed) >= limit:
            out.append(json.dumps(e, ensure_ascii=False))
            continue
        res = compose("archive", ts, check_archive(e, rub), rub, e.get("kind") or "")
        e["score"] = {"value": res["score"], "flags": res["flags"],
                      "panel": (res.get("panel") or {}).get("status", "done"),
                      "at": res["ts"]}
        scored.add(ts)
        changed.append(res)
        write_history("archive", res)
        out.append(json.dumps(e, ensure_ascii=False))

    if commit and changed:
        bak = ARCHIVE.with_suffix(f".jsonl.bak.score_{now().strftime('%Y%m%d_%H%M%S')}")
        try:
            shutil.copy2(ARCHIVE, bak)
        except Exception:
            pass
        ARCHIVE.write_text("\n".join(out) + "\n", encoding="utf-8")
        _save_scored(scored)
    return changed


def summary(n: int = 20) -> dict:
    if not HISTORY_FILE.exists():
        return {"rounds": 0}
    rows = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    rows = rows[-n:]
    if not rows:
        return {"rounds": 0}
    by_layer, by_kind, flags = {}, {}, {}
    for r in rows:
        s = float(r.get("score") or 0)
        by_layer.setdefault(r.get("layer"), []).append(s)
        k = r.get("kind") or "-"
        by_kind.setdefault(k, []).append(s)
        for f in r.get("flags") or []:
            flags[f] = flags.get(f, 0) + 1
    avg = lambda xs: round(sum(xs) / len(xs), 1)
    return {
        "rounds": len(rows),
        "last": round(float(rows[-1].get("score") or 0), 1),
        "by_layer": {k: {"n": len(v), "avg": avg(v)} for k, v in by_layer.items()},
        "by_kind": {k: {"n": len(v), "avg": avg(v)} for k, v in by_kind.items()},
        "top_flags": sorted(flags.items(), key=lambda x: -x[1])[:8],
        "panel_done": sum(1 for r in rows if (r.get("panel") or {}).get("score") is not None),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decision", nargs="?", const="latest", help="决策文件路径，或 latest")
    ap.add_argument("--archive", action="store_true", help="补评 archive 里未评的条目")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.summary:
        print(json.dumps(summary(), ensure_ascii=False, indent=2))
        return 0

    if args.decision:
        if args.decision == "latest":
            res = score_latest_decision()
        else:
            p = Path(args.decision)
            dec = json.loads(p.read_text(encoding="utf-8"))
            res = score_decision(dec)
            dec["score"] = res
            p.write_text(json.dumps(dec, ensure_ascii=False, indent=2), encoding="utf-8")
            write_history("decision", res)
        if res is None:
            print("没有决策文件")
            return 1
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
              f"决策分 {res['score']}  flags={res['flags'] or '无'}  面板={res['panel'].get('status', 'done')}")
        return 0

    if args.archive:
        changed = score_archive(commit=not args.dry)
        for r in changed:
            print(f"[{r['target_ts']}] {r.get('kind') or '-':10s} {r['score']:5.1f}  flags={r['flags']}")
        if not changed:
            print("archive 没有新条目需要评分")
        return 0

    # 默认：决策 + archive 都跑一遍
    res = score_latest_decision()
    changed = score_archive()
    if res:
        print(f"决策分 {res['score']}  flags={res['flags'] or '无'}")
    print(f"archive 新评 {len(changed)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
