# -*- coding: utf-8 -*-
"""装机猿片单 —— 冷库全量 + 候选池小窗口滚动。

背景：远古时代装机猿(UID 35359510) 全站 1051 个视频已经抓进
self/hardware/ape_videos.json（2016-10 至今）。1051 条不可能一次全塞进
goals_curiosity.json —— 池子只有几十条容量，塞满会把热搜信号全挤掉，
而且每条都要写一条 curiosity.md，学不完也写不完。

所以：冷库存全量，候选池只放 APE_WINDOW 条"当前在学的"。
吃完（被 iter_cycle 挑走的 id 会进 iter_out/decision_*.json）就补下一批，
冷库里没学过的按发布时间从新到旧顺位往下。

用法：
  python self/ape_pool.py            # 补窗口（默认补到 8 条）
  python self/ape_pool.py --n 12     # 指定窗口大小
  python self/ape_pool.py --show     # 看当前窗口和冷库剩余
  python self/ape_pool.py --reset    # 清空池里的 ape 条目（冷库不动）
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
ROOT = SELF.parent
LIB = SELF / "hardware" / "ape_videos.json"
POOL = SELF / "goals_curiosity.json"
ITER_OUT = SELF / "iter_out"
STAMP = SELF / "hardware" / "ape_pool_state.json"

UID = 35359510
UP = "远古时代装机猿"
APE_WINDOW = 8
APE_TTL_H = 24 * 30.0          # 常驻片单：30 天不学就换掉，别在池里烂着
SCAN_FILES = 400


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
        if p.get("kind") == "curiosity" and p.get("id"):
            ids.add(str(p["id"]))
    return ids


def load_lib() -> dict:
    if not LIB.exists():
        raise SystemExit(f"冷库不存在：{LIB}（先跑抓取脚本）")
    return json.loads(LIB.read_text(encoding="utf-8-sig"))


def load_pool() -> dict:
    if POOL.exists():
        try:
            return json.loads(POOL.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"items": []}


def is_ape(it: dict) -> bool:
    return str(it.get("id", "")).startswith("ape_") or it.get("source") == UP


def item_of(v: dict) -> dict:
    title = v["title"]
    short = title if len(title) <= 60 else title[:58] + "…"
    return {
        "id": "ape_" + v["bvid"],
        "kind": "curiosity",
        "title": f"硬件：{short}（装机猿 {v.get('date','')}）",
        "keyword": title,
        "curiosity_cat": "硬件技术",
        "priority": 3,
        "cost_rmb": 0,
        "source": UP,
        "source_url": f"https://www.bilibili.com/video/{v['bvid']}",
        "added_at": now_str(),
        "ttl_h": APE_TTL_H,
        "tier": "t2_write_low",
        "verify": (
            f"看完装机猿《{short}》后写进 self/curiosity.md：一条当日条目，三段——"
            "【讲了什么】看懂的技术点/型号/参数结论；【关键数字】价格、功耗、帧数、代际差等硬数据；"
            "【我能用到哪】对装机选型、硬件行情或我自己的机器怎么用。至少 150 字并附视频链接；"
            "看完觉得没价值的也要留一行『不值得』，不许空手结项"
        ),
    }


def fill(n: int = APE_WINDOW) -> dict:
    lib = load_lib()
    pool = load_pool()
    items = [it for it in (pool.get("items") or []) if isinstance(it, dict)]
    done = consumed_ids()

    keep, live_ape = [], []
    for it in items:
        if not is_ape(it):
            keep.append(it)
            continue
        # 吃过 / 冷库里没了的，直接清掉
        if str(it.get("id")) in done or not str(it.get("id", "")).startswith("ape_"):
            continue
        live_ape.append(it)

    live_ids = {str(it.get("id")) for it in live_ape}
    need = max(0, int(n) - len(live_ape))
    added = []
    if need:
        for v in sorted(lib.get("videos") or [], key=lambda x: -int(x.get("created") or 0)):
            if len(added) >= need:
                break
            if "ape_" + v["bvid"] in done or "ape_" + v["bvid"] in live_ids:
                continue
            it = item_of(v)
            added.append(it)
            live_ids.add(it["id"])

    pool["items"] = keep + live_ape + added
    if "generated_at" not in pool:
        pool["generated_at"] = now_str()
    POOL.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")

    total = int(lib.get("count") or len(lib.get("videos") or []))
    learned = len([i for i in done if i.startswith("ape_")])
    st = {
        "ts_unix": time.time(), "ts": now_str(),
        "lib_total": total, "learned": learned,
        "window": len(live_ape) + len(added), "added": len(added),
        "queue_title": (added[0]["title"] if added else (live_ape[0]["title"] if live_ape else None)),
    }
    STAMP.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"added": len(added), "window": st["window"], "learned": learned,
            "left": max(0, total - learned - (len(live_ape) + len(added))), "total": total}


def show() -> None:
    lib = load_lib()
    pool = load_pool()
    ape = [it for it in (pool.get("items") or []) if isinstance(it, dict) and is_ape(it)]
    done = {i for i in consumed_ids() if i.startswith("ape_")}
    total = int(lib.get("count") or 0)
    print(f"冷库 {total} 条（{UP}）｜已学 {len(done)}｜窗口 {len(ape)}｜池子里其它条目 "
          f"{len([i for i in (pool.get('items') or []) if not is_ape(i)])}")
    for i, it in enumerate(ape, 1):
        print(f"  {i}. [{it.get('priority')}] {it.get('title')}  {it.get('source_url')}")


def reset() -> None:
    pool = load_pool()
    pool["items"] = [it for it in (pool.get("items") or []) if not is_ape(it)]
    POOL.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    print("池里的 ape 条目已清空（冷库没动）")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--show" in args:
        show()
    elif "--reset" in args:
        reset()
    else:
        n = APE_WINDOW
        if "--n" in args:
            n = int(args[args.index("--n") + 1])
        r = fill(n)
        print(json.dumps(r, ensure_ascii=False))
