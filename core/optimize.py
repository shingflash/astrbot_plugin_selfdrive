# -*- coding: utf-8 -*-
"""优化池料源：加法（补齐最小功能单元）与减法（去重/去无关）两类候选。

主人 2026-11-17 18:35 的口径：
  · 加法：从最小功能单元开始、一级一级往上修；功能实现越正确分越高，
    偏离原设计（同名不同义、参数对不上 docstring）分越低
  · 减法：能不能复用、复用会不会带来带宽/延迟、功能是否与原本设计无关；
    动代码前先备份（*.bak_YYYYMMDD_HHMM）

只读源码（ast 解析），不执行目标脚本、不联网、不烧钱。
输出 self/goals_optimize.json，由 iter_cycle.py 装配候选时读入：
kind=optimize 与 kind=meta 同归 optimize 池。

用法:
  python optimize.py            看缓存/过期就重扫
  python optimize.py --force    强制重扫
  python optimize.py --json     打 JSON
  python optimize.py --variants 看每个候选的两种改法
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WS = HERE.parent
OUT = HERE / "goals_optimize.json"
CST = timezone(timedelta(hours=8))
TTL_H = 6.0
MAX_ITEMS = 8

SCAN_DIRS = [HERE, WS / "tools"]
BIG_FUNC_LINES = 100     # 超过这么多行的函数算「没拆到最小单元」
IO_HINT = re.compile(r"(read|write|load|save|dump|fetch|http|request|sql|open|poll)", re.I)
GENERIC = {"main", "__init__", "run", "setup", "load", "save", "loads", "dumps",
           "get", "set", "check", "build", "make", "test", "read", "write", "walk"}
SKIP_FILES = {"optimize.py"}   # 自己不进候选
SKIP_PAT = re.compile(r"(\.bak|backup|_v\d+\.py$)", re.I)   # 备份/旧版不参与优化
# 减法禁区（2026-11-17 主人：减法排除核心功能）：链路本体 + 链路直接依赖的模块，
# 一律不许进减法候选。权重也压低，加法律永远优先 —— 不然减到最后什么都不剩。
CORE_FILES = {"iter_cycle.py", "switchboard.py", "wallet.py"}
SUB_WEIGHT = 0.70


def _now() -> datetime:
    return datetime.now(CST)


def _hid(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def _py_files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        if d.is_dir():
            out += [p for p in sorted(d.glob("*.py"))
                    if p.name not in SKIP_FILES and not SKIP_PAT.search(p.name)]
    return out


def _parse(p: Path) -> dict:
    """解析一个文件的函数/类清单。解析失败返回空，绝不抛。"""
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except Exception:
        return {"path": p, "src": "", "funcs": [], "lines": 0}
    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            funcs.append({
                "name": node.name,
                "line": start,
                "end": end,
                "size": max(0, end - start + 1),
                "doc": (ast.get_docstring(node) or "").strip().replace("\n", " ")[:100],
                "args": len(node.args.args),
                "is_test": p.name.startswith("test_"),
            })
    return {"path": p, "src": src, "funcs": funcs,
            "lines": len(src.splitlines()), "tree": tree}


def _scan() -> list[dict]:
    return [_parse(p) for p in _py_files()]


def _chain_blob() -> str:
    """链路主文件全文，用来判「这脚本还在被用吗」。"""
    try:
        return (HERE / "iter_cycle.py").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _core_files(scan: list[dict], chain: str) -> set:
    """核心 = 链路本体 + 被 iter_cycle 直接 import 的模块。减法不许碰。"""
    core = set(CORE_FILES)
    for p in scan:
        stem = re.escape(p["path"].stem)
        if re.search(rf"(?m)^\s*(?:import|from)\s+{stem}\b", chain):
            core.add(p["path"].name)
    return core


# ---------------------------------------------------------------- 评分
def _score(level: str, has_test: bool, has_doc: bool, in_chain: bool, dev: float) -> float:
    """功能正确度↑、偏离设计↓。

    level：func 最小单元 / file 整个脚本 / chain 链路一层。
    dev：偏离分 0~1（同名不同义、docstring 对不上、跨层越权）。
    """
    base = {"func": 0.50, "file": 0.62, "chain": 0.72}.get(level, 0.5)
    s = base + (0.20 if has_test else 0.0) + (0.08 if has_doc else 0.0) \
        + (0.10 if in_chain else 0.0) - 0.45 * dev
    return round(max(0.0, min(1.0, s)), 2)


def _mk(src: str, title: str, *, kind: str = "optimize", level: str = "func",
        mode: str = "add", unit: str = "", evidence: str = "", verify: str = "",
        variants: list | None = None, score: float = 0.5, cost: float = 0.1,
        io_risk: str = "", weight: float = 1.0) -> dict:
    eff = round(max(0.0, min(1.0, score * weight)), 2)
    return {
        "id": ("opt_" if kind == "optimize" else "meta_") + _hid(src + "|" + title),
        "title": title,
        "kind": kind,
        "mode": mode,                 # add 加法 / sub 减法
        "level": level,               # func / file / chain
        "unit": unit,                 # 最小功能单元名
        "cost_rmb": cost,
        "priority": max(1, min(6, round(2 + eff * 4))),
        "tier": "t2_write_low",
        "src": src,
        "evidence": evidence,
        "verify": verify,
        "variants": variants or [],
        "score": eff,
        "score_raw": score,
        "weight": weight,
        "io_risk": io_risk,
    }


# ---------------------------------------------------------------- 加法
def add_no_test(scan: list[dict], chain: str) -> list[dict]:
    """没单测的核心脚本：先补最小单测锁行为，再按设计修。"""
    tested = {p["path"].name[len("test_"):] for p in scan if p["path"].name.startswith("test_")}
    out = []
    for p in scan:
        name = p["path"].name
        if name.startswith("test_") or name in {t for t in tested}:
            continue
        if f"{name[:-3]}" in tested or f"test_{name}" in {q["path"].name for q in scan}:
            continue
        fns = [f for f in p["funcs"] if not f["is_test"] and f["name"] not in GENERIC]
        if not fns:
            continue
        hit = f"{name[:-3]}" in chain
        if not hit and len(fns) < 6:
            continue          # 既不在链路、函数又少的，先不管
        target = max(fns, key=lambda f: f["size"])
        s = _score("func", False, bool(target["doc"]), hit, 0.0)
        out.append(_mk(
            f"{name}:{target['name']}",
            f"加法：给 {name} 的 {target['name']}() 补最小单测（{p['lines']} 行脚本无 test）",
            level="func", mode="add", unit=f"{name}:{target['name']}",
            evidence=f"{name}:{target['line']} {target['name']}() {target['size']} 行，无对应 test_*.py；"
                     f"链路引用 {'有' if hit else '无'}",
            verify=f"① 先备份 {name} → {name}.bak_YYYYMMDD_HHMM；"
                   f"② 新建 self/test_{name[:-3]}.py，只测 {target['name']}() 的输入输出（3 个用例：正常/边界/异常），"
                   f"断言必须机械可跑；③ python -X utf8 test_{name[:-3]}.py 全 PASS 才算锁住；"
                   f"④ 再改一处实现，测试仍 PASS 才算「行为没坏」",
            variants=[
                f"A 锁行为：先写单测固定现在输出，什么都不改（安全，先做这个）",
                f"B 按设计修：单测暴露与 docstring 不一致的地方再动手改 {target['name']}()",
            ],
            score=s))
    return out


def add_big_func(scan: list[dict], chain: str) -> list[dict]:
    """超长函数：没拆到最小功能单元，一级一级往下拆。"""
    out = []
    for p in scan:
        for f in p["funcs"]:
            if f["size"] <= BIG_FUNC_LINES or f["name"].startswith("__"):
                continue
            hit = f["name"] in chain
            s = _score("func", False, bool(f["doc"]), hit, 0.25)
            out.append(_mk(
                f"{p['path'].name}:{f['name']}",
                f"加法：{p['path'].name} 的 {f['name']}() 有 {f['size']} 行，拆成最小单元",
                level="func", mode="add", unit=f"{p['path'].name}:{f['name']}",
                evidence=f"{p['path'].name}:{f['line']}-{f['end']}，{f['size']} 行，参数 {f['args']} 个",
                verify=f"① 备份 {p['path'].name}；② 按「一个函数只做一件事」切成 2~3 段，"
                       f"每段入口带 docstring 写明输入输出；③ py_compile rc0 且拆分前后对同一输入的输出逐字相同；"
                       f"④ 拆完总行数不得增加超过 15%（否则是搬代码不是拆责任）",
                variants=[
                    "A 先抽纯计算段（无 IO 的那几行）成独立函数，主函数只剩编排",
                    "B 先抽判据段（各种 if 条件）成 should_*()，主函数只剩调度",
                ],
                score=s))
    return out


# ---------------------------------------------------------------- 减法
def sub_dup_defs(scan: list[dict], core: set) -> list[dict]:
    """同名函数在多文件各写一份：能不能复用。核心模块不参与。"""
    where: dict[str, list[tuple[str, dict]]] = {}
    for p in scan:
        for f in p["funcs"]:
            if f["is_test"] or f["name"] in GENERIC or len(f["name"]) <= 2:
                continue
            where.setdefault(f["name"], []).append((p["path"].name, f))
    out = []
    for name, hits in where.items():
        files = {h[0] for h in hits}
        if len(files) < 2:
            continue
        if files & core:
            continue          # 命中核心模块：不许合并，核心实现保持原样
        total = sum(h[1]["size"] for h in hits)
        if total < 12:
            continue
        # 同名不同义：参数个数差太大、或规模差 3 倍以上，合了就是把两件事焊一起
        sizes = [max(1, h[1]["size"]) for h in hits]
        args = [h[1]["args"] for h in hits]
        if max(sizes) > 3 * min(sizes) or max(args) - min(args) > 2:
            continue
        io = "io" if IO_HINT.search(name) else ""
        locs = "、".join(f"{fn}:{f['line']}" for fn, f in hits[:4])
        s = _score("file", False, bool(hits[0][1]["doc"]), False, 0.15)
        out.append(_mk(
            f"dup:{name}",
            f"减法：{name}() 在 {len(files)} 个文件各写一份（共 {total} 行），抽公共",
            level="file", mode="sub", unit=f"dup:{name}",
            evidence=f"{locs}" + ("" if not io else "；含 IO 关键字"),
            io_risk="io" if io else "",
            verify=(f"① 先比一遍各份实现：语义不同就**不许**合并，只把差异记进 skills；语义相同再抽。"
                    f"② 抽到 self/util.py 后逐个改 import，py_compile 全 rc0。"
                    f"③ 复用前后对同一输入跑 1000 次 timeit，{name} 属 {'IO 类，抽公共只省代码不省延迟，别声称变快' if io else '纯计算类，允许声称变快'}。"
                    f"④ 备份每个被改文件"),
            variants=[
                "A 抽公共 + 各处 import（改动面大，可回滚）",
                "B 只保留一处实现，其余改成调用（改动面小，但要求调用方在同一进程）",
            ],
            score=s, weight=SUB_WEIGHT))
    return out


def sub_unused(scan: list[dict], chain: str, core: set) -> list[dict]:
    """定义了但工作区里没人引用的函数：与原本设计无关的代码。核心文件一律豁免。"""
    allsrc = "\n".join(p["src"] for p in scan) + "\n" + chain
    out = []
    for p in scan:
        if p["path"].name.startswith("test_") or p["path"].name in core:
            continue          # 核心功能不在减法范围
        for f in p["funcs"]:
            n = f["name"]
            if f["is_test"] or n.startswith("__") or n in GENERIC or len(n) <= 4:
                continue
            used = len(re.findall(rf"\b{re.escape(n)}\b", allsrc))
            # 定义 1 次 + 没人调 → used == 1
            if used > 1:
                continue
            if f["size"] < 6:
                continue
            s = _score("func", False, bool(f["doc"]), False, 0.35)
            out.append(_mk(
                f"dead:{p['path'].name}:{n}",
                f"减法：{p['path'].name} 的 {n}() 全工作区无人调用（{f['size']} 行），确认后删",
                level="func", mode="sub", unit=f"{p['path'].name}:{n}",
                evidence=f"{p['path'].name}:{f['line']}，全量文本里仅出现 {used} 次（定义处）",
                verify=f"① 先 grep 全工作区（含 tools/、plugins/、.vbs）确认没有外部调用者；"
                       f"② 备份文件；③ 注释掉 {n}() 跑一遍链路 py_compile + iter_cycle --show rc0；"
                       f"④ 两轮无异常再真删，不许一上来就删",
                variants=[
                    "A 先注释（可秒回滚），观察两轮",
                    "B 直接删 + git/备份留底（要求 ① 已确认无调用者）",
                ],
                score=s, weight=SUB_WEIGHT))
    return out


# ---------------------------------------------------------------- 元（自迭代机制自身）
def meta_findings() -> list[dict]:
    """元问题清单：关着的开关、断掉的池子（纯数据，不建条目）。

    2026-11-17 主人：这一路并进自省轮（introspect），不再占优化池的桶位。
    优化池只留加法与减法；这里只负责把问题讲清楚，动作交给自省轮去推。
    """
    out = []
    sw = HERE / "switchboard.py"
    try:
        swsrc = sw.read_text(encoding="utf-8", errors="replace")
    except Exception:
        swsrc = ""
    # 关着的开关
    for sub, desc, on in re.findall(r'"(\w+)":\s*\("([^"]+)",\s*(True|False)\)', swsrc):
        if on == "False":
            out.append({
                "key": f"sw:{sub}",
                "title": f"开关 {sub} 还关着（{desc}），要么接上要么删",
                "evidence": f"switchboard.py 里 {sub} = False",
                "verify": f"① 先写一个最小用例复现「关着会导致什么」；② 补实现后把 {sub} 置 True；"
                          f"③ python -X utf8 test_switchboard.py 全 PASS；④ 置 True 跑一次 iter_cycle --show 确认不炸",
                "variants": ["A 真接上：补实现 + 置 True + 加用例",
                             "B 明确废弃：从 CHAINS/BLOCKS 里删掉这条，别留假开关"],
                "score": _score("chain", False, True, True, 0.1),
            })
    # 池子断链
    try:
        ic = (HERE / "iter_cycle.py").read_text(encoding="utf-8", errors="replace")
    except Exception:
        ic = ""
    if "practice" not in ic:
        out.append({
            "key": "pool:practice",
            "title": "practice_lib.json 没进 POOL_GROUPS，3 条任务永远选不中",
            "evidence": "practice_lib.json 3 条无 kind 字段，pool_of() 返回 None",
            "verify": "① 给 3 条补 kind=practice；② POOL_GROUPS 加 practice 组与 POOL_ORDER 位置；"
                      "③ 跑 iter_cycle --rotation 确认新池出现在轮转里；④ 备份 iter_cycle.py",
            "variants": ["A 单独成第 7 池", "B 并进 explore 池（省一轮）"],
            "score": _score("chain", False, True, True, 0.2),
        })
    return out


# ---------------------------------------------------------------- 组装
def build(write: bool = True) -> dict:
    scan = _scan()
    chain = _chain_blob()
    core = _core_files(scan, chain)
    items: list[dict] = []
    for fn in (lambda: add_no_test(scan, chain),
               lambda: add_big_func(scan, chain),
               lambda: sub_dup_defs(scan, core),
               lambda: sub_unused(scan, chain, core)):
        try:
            items += fn()
        except Exception as e:
            print(f"[optimize] 某一路失败: {e}", file=sys.stderr)
    # 同 id 去重，按分数排
    seen, uniq = set(), []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
    uniq.sort(key=lambda x: (-x["score"], x["id"]))
    # 分桶取数：加法必须多于减法（2026-11-17 主人：不然减到最后什么都没留下）
    # meta 一路已并进自省轮（introspect），这里只剩加 / 减两种。
    subs = [x for x in uniq if x["mode"] == "sub"][:2]
    adds = [x for x in uniq if x["mode"] == "add"][:5]
    uniq = (adds + subs)[:MAX_ITEMS]
    data = {
        "generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "ttl_h": TTL_H,
        "sources": ["add_no_test", "add_big_func", "sub_dup_defs", "sub_unused"],
        "items": uniq,
    }
    if write:
        try:
            OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    return data


def load(force: bool = False) -> list:
    d = {}
    if OUT.exists() and not force:
        try:
            d = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            d = {}
    fresh = False
    try:
        ts = datetime.strptime(str(d.get("generated_at")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        fresh = (_now() - ts).total_seconds() < float(d.get("ttl_h") or TTL_H) * 3600
    except Exception:
        fresh = False
    if force or not fresh or not d.get("items"):
        d = build(write=True)
    return list(d.get("items") or [])


if __name__ == "__main__":
    force = "--force" in sys.argv
    if "--json" in sys.argv:
        print(json.dumps(load(force=force), ensure_ascii=False, indent=1))
    elif "--variants" in sys.argv:
        for it in load(force=force):
            print(f"[{it['kind']}/{it['mode']}] {it['title']}  score={it['score']}")
            for v in it["variants"]:
                print("    " + v)
    else:
        items = load(force=force)
        print(f"优化池 {len(items)} 条")
        for it in items:
            print(f"  [{it['kind']}/{it['mode']}/{it['level']}] {it['score']} {it['title']}")
