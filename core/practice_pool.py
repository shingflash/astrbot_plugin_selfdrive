# -*- coding: utf-8 -*-
"""实践池（2026-11-10 主人派活）：把 AI 训练/评测这类东西放进自迭代候选池，类别统一叫「实践」。

背景：主人转来一份 MaxxBench 报告——所谓 DeepSWE 高分模型，在题目被微小改动后大面积翻车，
还有模型把任务外包给外部模型（偷用 API Key 联网、假扮仓库维护者）。
主人要求：DeepSWE 这类评测/训练做法，以及平时学到的东西，都进候选池，统一归类「实践」，
而且要动手做一次、留可验证产物，不是写一篇学习笔记。

设计：
  · 冷库 self/practice/practice_lib.json 存全部实践任务（每条自带 why 和 verify）。
  · 池子 goals_curiosity.json 只放 PRACTICE_WINDOW 条在学的，吃完（被 iter_cycle 挑走）再补下一批。
  · 条目仍是 kind=curiosity、curiosity_cat=实践，复用现有流水线，不新增 kind，避免 iter_cycle 崩。
  · 池子整体过期时：热搜条目本来就凉了，一并清掉；实践条目不随时间失效，保留。

用法：
  python self/practice_pool.py             # 补窗口（默认 3 条）
  python self/practice_pool.py --n 5       # 指定窗口
  python self/practice_pool.py --show      # 看窗口和冷库
  python self/practice_pool.py --reset     # 清池里的 prac 条目（冷库不动）
  python self/practice_pool.py --add 标题   # 手工加一条到冷库并进窗口
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:                                    # Windows 控制台默认 GBK，中文直接 print 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SELF = Path(__file__).resolve().parent
POOL = SELF / "goals_curiosity.json"
LIB = SELF / "practice" / "practice_lib.json"
LOG = SELF / "practice" / "log.md"
ITER_OUT = SELF / "iter_out"
STAMP = SELF / "practice" / "practice_pool_state.json"

CAT = "实践"
ID_PREFIX = "prac_"
PRACTICE_WINDOW = 3
TTL_H = 24 * 30.0                       # 实践任务不是热点，挂一个月不学才换
SCAN_FILES = 400
CST = timezone(timedelta(hours=8))


def now_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def load_lib() -> dict:
    if not LIB.exists():
        raise SystemExit(f"冷库不存在：{LIB}")
    d = json.loads(LIB.read_text(encoding="utf-8-sig"))
    if not isinstance(d.get("tasks"), list):
        raise SystemExit("冷库格式不对：缺 tasks")
    return d


def save_lib(d: dict) -> None:
    d["count"] = len(d.get("tasks") or [])
    d["updated_at"] = now_str()
    LIB.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def load_pool() -> dict:
    if POOL.exists():
        try:
            d = json.loads(POOL.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict) and isinstance(d.get("items"), list):
                return d
        except Exception:
            pass
    return {"generated_at": now_str(), "ttl_h": 48.0, "sources": {}, "dropped": {}, "items": []}


def consumed_ids() -> set:
    ids = set()
    if not ITER_OUT.exists():
        return ids
    for f in sorted(ITER_OUT.glob("decision_*.json"))[-SCAN_FILES:]:
        try:
            j = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        p = j.get("pick") or {}
        if p.get("id"):
            ids.add(str(p["id"]))
    return ids


def is_prac(it: dict) -> bool:
    return str(it.get("id", "")).startswith(ID_PREFIX) or it.get("curiosity_cat") == CAT


def item_of(t: dict) -> dict:
    sid = str(t.get("sid") or t.get("title"))[:40]
    return {
        "id": ID_PREFIX + sid,
        "kind": "curiosity",
        "title": f"实践：{t.get('title')}",
        "keyword": t.get("title"),
        "curiosity_cat": CAT,
        "priority": int(t.get("priority") or 4),
        "cost_rmb": 0,
        "source": "自迭代实践池",
        "source_url": "",
        "ttl_h": TTL_H,
        "tier": "t2_write_low",
        "added_at": now_str(),
        "why": t.get("why") or "",
        "verify": t.get("verify") or (
            f"动手做完「{t.get('title')}」后写进 self/practice/log.md：做了什么、结果、"
            f"可复现命令或产物文件路径；做不成的写清卡在哪一步并附报错原文，不许空手结项"),
    }


def fill(n: int = PRACTICE_WINDOW) -> dict:
    lib = load_lib()
    pool = load_pool()
    done = consumed_ids()

    # 池子整体过期：热搜凉了清掉，实践条目留着
    reset_ts = False
    try:
        g = datetime.strptime(str(pool.get("generated_at"))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        if (datetime.now(CST) - g).total_seconds() > float(pool.get("ttl_h") or 48) * 3600:
            reset_ts = True
    except Exception:
        reset_ts = True
    if reset_ts:
        pool["items"] = [it for it in (pool.get("items") or []) if isinstance(it, dict) and is_prac(it)]
        pool["generated_at"] = now_str()

    items = [it for it in (pool.get("items") or []) if isinstance(it, dict) and it.get("id")]
    keep, live = [], []
    for it in items:
        if not is_prac(it):
            keep.append(it)
            continue
        if str(it.get("id")) in done:
            continue
        live.append(it)

    live_ids = {str(it.get("id")) for it in live}
    need = max(0, int(n) - len(live))
    added = []
    if need:
        for t in sorted(lib.get("tasks") or [], key=lambda x: -int(x.get("priority") or 0)):
            if len(added) >= need:
                break
            it = item_of(t)
            if it["id"] in done or it["id"] in live_ids:
                continue
            added.append(it)
            live_ids.add(it["id"])

    pool["items"] = keep + live + added
    pool["generated_at"] = pool.get("generated_at") or now_str()
    pool["updated_at"] = now_str()
    POOL.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")

    total = len(lib.get("tasks") or [])
    learned = len([i for i in done if i.startswith(ID_PREFIX)])
    st = {
        "ts_unix": time.time(), "ts": now_str(),
        "lib_total": total, "learned": learned,
        "window": len(live) + len(added), "added": len(added),
        "queue_title": (added[0]["title"] if added else (live[0]["title"] if live else None)),
    }
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"added": len(added), "window": st["window"], "learned": learned,
            "left": max(0, total - learned - (len(live) + len(added))), "total": total}


def show() -> None:
    lib = load_lib()
    pool = load_pool()
    prac = [it for it in (pool.get("items") or []) if isinstance(it, dict) and is_prac(it)]
    done = {i for i in consumed_ids() if i.startswith(ID_PREFIX)}
    print(f"冷库 {len(lib.get('tasks') or [])} 条｜已实践 {len(done)}｜窗口 {len(prac)}｜"
          f"池里其它条目 {len([i for i in (pool.get('items') or []) if not is_prac(i)])}")
    for i, it in enumerate(prac, 1):
        print(f"  {i}. [p{it.get('priority')}] {it.get('title')}")
    for i, it in enumerate(prac, 1):
        print(f"  {i}. verify: {str(it.get('verify'))[:80]}...")


def reset() -> None:
    pool = load_pool()
    pool["items"] = [it for it in (pool.get("items") or []) if not is_prac(it)]
    POOL.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    print("池里的实践条目已清空（冷库没动）")


def add(title: str, priority: int = 4) -> None:
    lib = load_lib()
    sid = "custom_" + str(int(time.time()))[-6:]
    lib["tasks"].append({
        "sid": sid, "title": title, "priority": int(priority), "why": "主人临时加",
        "verify": (f"动手做完「{title}」后写进 self/practice/log.md：做了什么、结果、"
                   f"可复现命令或产物文件路径；做不成的写清卡在哪一步并附报错原文"),
    })
    save_lib(lib)
    print(f"+ 已入冷库 {ID_PREFIX}{sid}：{title}")
    print(json.dumps(fill(), ensure_ascii=False))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--show" in args:
        show()
    elif "--reset" in args:
        reset()
    elif "--add" in args:
        i = args.index("--add")
        title = args[i + 1] if len(args) > i + 1 else ""
        if not title:
            raise SystemExit("--add 后面跟标题")
        add(title)
    else:
        n = PRACTICE_WINDOW
        if "--n" in args:
            n = int(args[args.index("--n") + 1])
        print(json.dumps(fill(n), ensure_ascii=False))
