# -*- coding: utf-8 -*-
"""好奇机制（2026-11-04 主人派活）：抓网络高频词 → 分类打分 → 过滤垃圾 → 写入候选池。

规则（主人定）：
  优先级：前沿科技5 > 现有技术4 > 热点话题3 > 政治经济2 > 黑话和梗1
  禁入：维稳/掩盖捂嘴类新闻、饭圈偶像类新闻 —— 命中直接丢，不进池

用法：
  python self/curiosity.py            # 抓一遍，写 self/goals_curiosity.json
  python self/curiosity.py --dry-run  # 只看会得到什么，不落盘
  python self/curiosity.py --show     # 打印当前池子

输出结构（iter_cycle.py 会读）：
  {"generated_at": "...", "sources": {...}, "items": [ {id,title,keyword,kind,...} ]}
  items 里每条带 ttl_h，iter_cycle 读的时候会按 generated_at 判过期。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
if str(SELF) not in sys.path:
    sys.path.insert(0, str(SELF))

import topic_ingest   # 采集层：多源全量灌进 topic.db
import topic_store    # 存储层：去重 / 取题 / 用完剔除

OUT = SELF / "goals_curiosity.json"
CST = timezone(timedelta(hours=8))
TTL_H = 48.0          # 热点 48 小时就凉了，过期的候选别再占坑
MAX_ITEMS = 6         # 一次抓取最多丢 6 条进候选池，别把池子灌满
MAX_POOL_ITEMS = 12   # 池子总上限：热搜 6 条 + 刷视频等外部追加，超了按优先级砍
POOL_SCAN = 200       # 从 topic.db 一次捞多少条候选给分类器筛（2026-11-17 建库后加）
TIMEOUT = 6.0
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---- 分类表：(名字, 优先级, 关键词) ----
CATS = [
    ("前沿科技", 5, ("大模型", "ai", "人工智能", "agi", "芯片", "算力", "量子", "可控核聚变", "核聚变",
                   "机器人", "具身智能", "火箭", "卫星", "航天", "脑机", "基因", "crispr", "半导体", "光刻",
                   "自动驾驶", "人形机器人", "空间站", "6g", "光刻机", "新材料", "超导")),
    ("现有技术", 4, ("显卡", "gpu", "cpu", "手机", "笔记本", "相机", "开源", "固件", "驱动", "系统更新",
                   "鸿蒙", "安卓", "linux", "windows", "路由器", "nas", "显示器", "固态", "内存条",
                   "单片机", "树莓派", "3d打印", "无人机", "耳机", "充电")),
    ("政治经济", 2, ("关税", "利率", "降息", "加息", "汇率", "股市", "a股", "美股", "房价", "楼市", "央行",
                   "经济", "gdp", "通胀", "cpi", "财政", "失业率", "供应链", "出口", "进口", "制裁")),
    ("黑话和梗", 1, ("梗", "整活", "抽象", "乐子", "绷不住", "破防", "急了", "梗图", "表情包", "网友",
                   "上热搜", "弹幕", "鬼畜", "二创", "解读向", "真香", "无语")),
    # 实践类（2026-11-10 主人派活）：关键词留空，热搜永远不会被分到这里，
    # 只用于 --add --cat 实践 手工追加，以及 practice_pool.py 直接写池的合法分类。
    ("实践", 5, ()),
]

# ---- 禁入名单（命中即丢）----
BAN_CATEGORY = {
    "维稳捂嘴": ("辟谣", "官方通报", "已依法", "依法查处", "专项整治", "舆情", "正能量", "宣传活动",
               "表彰", "表彰大会", "座谈", "部署会", "专题会议", "贯彻落实", "主题教育", "严打",
               "清朗", "封号", "限流", "删帖", "澄清", "致敬", "缅怀"),
    "饭圈偶像": ("明星", "偶像", "爱豆", "饭圈", "粉丝", "恋情", "塌房", "官宣", "综艺", "演唱会",
               "选秀", "男团", "女团", "应援", "打榜", "超话", "解约", "同框", "路透", "生图",
               "造型", "红毯", "塌了", "私生活"),
}


def _day() -> str:
    """id 里带日期：同一个高频词同一天只触发一次，第二天还热才允许再做一遍。"""
    return datetime.now(CST).strftime("%Y%m%d")


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return r.read().decode("utf-8", "replace")


def bili_hot() -> list[dict]:
    d = json.loads(_get("https://api.bilibili.com/x/web-interface/search/square?limit=30"))
    lst = ((d.get("data") or {}).get("trending") or {}).get("list") or []
    out = []
    for n, it in enumerate(lst, 1):
        w = str(it.get("keyword") or "").strip()
        if w:
            out.append({"word": w, "rank": n, "source": "B站热搜",
                        "url": "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(w)})
    return out


def baidu_hot() -> list[dict]:
    d = json.loads(_get("https://top.baidu.com/api/board?platform=wise&tab=realtime"))
    out: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            w = node.get("word") or node.get("query")
            if isinstance(w, str) and w.strip():
                u = node.get("url") or ("https://www.baidu.com/s?wd=" + urllib.parse.quote(w.strip()))
                if all(x["word"] != w.strip() for x in out):
                    out.append({"word": w.strip(), "rank": len(out) + 1, "source": "百度热搜", "url": u})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk((d.get("data") or {}).get("cards") or [])
    return out[:30]


_ASCII_SHORT = re.compile(r"^[a-z0-9+.#-]{1,4}$")


def _kw_hit(low: str, kw: str) -> bool:
    """短英文词必须按词边界匹配。

    子串匹配会把 "ai" 命中 failures / training / rain，把 "gpu" 命中别的东西，
    2026-11-17 实测 "Three small failures…" 被当成 AI 前沿给了 p5。
    """
    if _ASCII_SHORT.match(kw):
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", low) is not None
    return kw in low


def classify(word: str, rank: int, kind: str = "") -> tuple[str, int]:
    low = word.lower()
    for name, pri, kws in CATS:
        if any(_kw_hit(low, k) for k in kws):
            return name, pri
    # 没命中关键词，就按来源兜底：顶刊论文天然算前沿、开源/技术站算现有技术
    if kind == "sci":
        return "前沿科技", 3
    if kind in ("tech", "lab"):
        return "现有技术", 3
    # 热榜前 10 位给个「热点话题」，后面的当噪声丢掉；分给 p2，让位给顶刊/开源项目
    if kind == "hot" and rank <= 10:
        return "热点话题", 2
    return "", 0


def banned(word: str) -> tuple[bool, str]:
    low = word.lower()
    for name, kws in BAN_CATEGORY.items():
        if any(k in low for k in kws):
            return True, name
    return False, ""


def build(dry: bool = False) -> dict:
    words: list[dict] = []
    src_stat: dict[str, int] = {}
    ing: dict = {}

    # 1) 用完剔除：把上一次池子里真被吃掉的候选从库里划掉
    try:
        cleared = topic_store.sync_from_decisions()
        if cleared:
            src_stat["_cleared"] = cleared
    except Exception as e:
        print(f"[warn] sync_from_decisions 失败: {e}")

    # 2) 采集多源热点入库（共享池；带冷却，别每次重抓 25 个源）
    if not dry:
        try:
            ing = topic_ingest.maybe_run()
            src_stat["_ingest"] = ing.get("rows", 0)
        except Exception as e:
            print(f"[warn] 采集失败: {e}")
            src_stat["_ingest"] = -1

    # 3) 从库里取候选——不限 kind，热榜/媒体/论文/实验室全给，交给下面分类器筛
    try:
        pool = topic_store.pick(POOL_SCAN, kinds=())
    except Exception as e:
        pool = []
        print(f"[warn] 取库失败: {e}")
    for i, r in enumerate(pool):
        words.append({"word": r["title"], "source": r["source"], "url": r["url"], "kind": r["kind"],
                      "rank": i + 1, "dkey": r["dkey"], "n_src": int(r.get("n_src") or 1)})
        src_stat[r["source"]] = src_stat.get(r["source"], 0) + 1

    # 4) 库空/没料 → 退回老路（B站 + 百度热搜），保证不会空手
    if not words:
        for fn in (bili_hot, baidu_hot):
            try:
                got = fn()
                for _w in got:
                    _w.setdefault("kind", "hot")
                words.extend(got)
                src_stat[fn.__name__] = len(got)
            except Exception as e:  # 单个源挂了不影响另一个
                src_stat[fn.__name__] = -1
                print(f"[warn] {fn.__name__} 抓取失败: {e}")

    seen: set[str] = set()
    items: list[dict] = []
    dropped = {"ban": 0, "noise": 0, "dup": 0}
    drop_detail: list[str] = []
    for w in words:
        word = w["word"]
        key = word.strip().lower()
        if key in seen:
            dropped["dup"] += 1
            continue
        seen.add(key)
        is_ban, why = banned(word)
        if is_ban:
            dropped["ban"] += 1
            if len(drop_detail) < 20:
                drop_detail.append(f"{why}:{word}")
            continue
        cat, pri = classify(word, int(w["rank"]), str(w.get("kind") or ""))
        if not cat:
            dropped["noise"] += 1
            continue
        n_src = int(w.get("n_src") or 1)
        # 跨源共振：同一件事被两个以上源同时报，才配叫热点
        if n_src >= 3:
            pri = min(5, pri + 2)
        elif n_src == 2:
            pri = min(5, pri + 1)
        dk = w.get("dkey") or ""
        hid = "c_" + (dk[:10] if dk else hashlib.md5(
            f"{cat}:{word}:{_day()}".encode("utf-8")).hexdigest()[:10])
        shown = word if len(word) <= 90 else word[:88] + "…"
        items.append({
            "id": hid,
            "dkey": dk,
            "kind": "curiosity",
            "title": f"好奇：{shown}（{cat}·{w['source']}" + (f"·{n_src}源共振" if n_src >= 2 else "") + "）",
            "keyword": word[:90],
            "curiosity_cat": cat,
            "priority": pri,
            "n_src": n_src,
            "cost_rmb": 0,
            "source": w["source"],
            "source_url": w["url"],
            "ttl_h": TTL_H,
            "tier": "t2_write_low",
            "verify": (f"读完/查完「{word[:60]}」后写进 self/curiosity.md，条目必须落成至少一条可用的东西："
                       f"①一条能马上用到自己流程/工具上的改动（写清改哪个文件、哪一行、改成什么），或"
                       f"②一次判断修正（原本我怎么想→现在怎么改）。【是什么】【为什么现在热】压到 150 字内当背景。"
                       f"两条都落不了的，在条目末尾写'用不上：<原因>'，这轮按 negative 记，不算有效轮"),
        })

    # 排序 = 分类分*10 + 跨源共振*6（同一件事几个源都报才算真热）
    items.sort(key=lambda x: (-(int(x["priority"]) * 10 + int(x.get("n_src") or 1) * 6),
                              x["source"], x["source_url"]))
    # 分类轮转取题：先让每个领域各出一条，不够再补第二轮。
    # 只按分数取的话，GitHub 项目能把「前沿科技」两个名额吃光，顶刊一条都进不来。
    buckets: dict[str, list] = {}
    for it in items:
        buckets.setdefault(it["curiosity_cat"], []).append(it)
    cat_order = sorted(buckets, key=lambda c: -max(
        int(x["priority"]) * 10 + int(x.get("n_src") or 1) * 6 for x in buckets[c]))
    per_src: dict[str, int] = {}
    per_cat: dict[str, int] = {}
    taken: set = set()
    capped: list[dict] = []
    for _rnd in range(3):
        if len(capped) >= MAX_ITEMS:
            break
        for c in cat_order:
            if len(capped) >= MAX_ITEMS:
                break
            if c == "热点话题" and _rnd == 0:
                continue   # 热榜碎片先让位，池子填不满才拿它们补位
            for it in buckets[c]:
                if it["id"] in taken:
                    continue
                s = it["source"]
                if per_src.get(s, 0) >= 2:   # 同源最多 2 条
                    continue
                if per_cat.get(c, 0) >= 2:   # 同分类最多 2 条
                    continue
                capped.append(it)
                taken.add(it["id"])
                per_src[s] = per_src.get(s, 0) + 1
                per_cat[c] = per_cat.get(c, 0) + 1
                break

    doc = {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "ttl_h": TTL_H,
        "sources": src_stat,
        "dropped": dropped,
        "dropped_detail": drop_detail,
        "items": capped,
    }
    if not dry and OUT.exists():
        # 热搜刷新不许冲掉外部追加的条目（灵感 kind=inspiration 那条腿）：
        # 它们不是从热搜来的，重抓一次热搜就把它们抹掉的话，灵感池永远开不了张。
        try:
            old = json.loads(OUT.read_text(encoding="utf-8-sig")).get("items") or []
        except Exception:
            old = []
        have = {str(it.get("keyword", "")).strip().lower() for it in capped}
        carry = [it for it in old
                 if isinstance(it, dict) and it.get("kind") in ("inspiration", "explore")
                 and str(it.get("keyword", "")).strip().lower() not in have]
        if carry:
            capped = (carry + capped)[:MAX_POOL_ITEMS]
            doc["items"] = capped   # 必须回写：capped 是新列表，不回写 carry 永远进不了文件
    if not dry:
        OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc



def _mk_item(word: str, cat: str, pri: int, source: str, url: str) -> dict:
    """跟 build() 里同一套结构，外面追加进来的词才跟热搜候选长得一样。"""
    return {
        "id": "c_" + hashlib.md5(f"{cat}:{word}:{_day()}".encode("utf-8")).hexdigest()[:8],
        "kind": "curiosity",
        "title": f"好奇：{word}（{cat}·{source}）",
        "keyword": word,
        "curiosity_cat": cat,
        "priority": pri,
        "cost_rmb": 0,
        "source": source,
        "source_url": url,
        "ttl_h": TTL_H,
        "tier": "t2_write_low",
        "added_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        # 实践类不是写笔记，是动手做一次并留可验证产物
        "verify": ((f"动手做完「{word}」后写进 self/practice/log.md：做了什么、结果、可复现命令或"
                    f"产物文件路径；做不成的写清卡在哪一步并附报错原文，不许空手结项")
                   if cat == "实践" else
                   (f"读完/查完「{word}」后写进 self/curiosity.md，条目必须落成至少一条可用的东西："
                    f"①一条能马上用到自己流程/工具上的改动（写清改哪个文件、哪一行、改成什么），或"
                    f"②一次判断修正（原本我怎么想→现在怎么改）。【是什么】【为什么现在热】压到 150 字内当背景。"
                    f"两条都落不了的，在条目末尾写'用不上：<原因>'，这轮按 negative 记，不算有效轮")),
    }


def add_items(words, cat: str | None = None, url: str = "", source: str = "B站观看") -> tuple:
    """外部追加好奇候选（AI 刷 B 站视频时用）：跟热搜同一套结构、同一个池子。

    规则：禁入词照样丢；同词同天只留一条（刷新时间不重复占坑）；
          池子整体过期就重置 generated_at，否则新加的词会被旧的时间戳一起判死。
    返回 (新增列表, 跳过列表)。
    """
    now_s = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    if OUT.exists():
        try:
            doc = json.loads(OUT.read_text(encoding="utf-8-sig"))
        except Exception:
            doc = {}
    else:
        doc = {}
    if not isinstance(doc, dict) or not doc:
        doc = {"generated_at": now_s, "ttl_h": TTL_H, "sources": {}, "dropped": {}, "items": []}
    try:
        g = datetime.strptime(str(doc.get("generated_at"))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        expired = (datetime.now(CST) - g).total_seconds() > TTL_H * 3600
    except Exception:
        expired = True
    if expired:
        doc = {"generated_at": now_s, "ttl_h": TTL_H, "sources": doc.get("sources") or {}, "dropped": {}, "items": []}

    items = [it for it in (doc.get("items") or []) if isinstance(it, dict) and it.get("id")]
    added, skipped = [], []
    valid_cats = {c[0]: c[1] for c in CATS}
    for w in words or []:
        word = str(w).strip()
        if not word:
            continue
        is_ban, why = banned(word)
        if is_ban:
            skipped.append(f"{why}:{word}")
            continue
        if any(str(it.get("keyword", "")).strip().lower() == word.lower() for it in items):
            skipped.append(f"dup:{word}")
            continue
        if cat and cat in valid_cats:
            c_name, pri = cat, valid_cats[cat]
        else:
            c_name, pri = classify(word, 1)
            if not c_name:
                c_name, pri = "热点话题", 3
        it = _mk_item(word, c_name, pri, source, url)
        items.append(it)
        added.append(it)

    items.sort(key=lambda x: (-int(x.get("priority") or 0), str(x.get("added_at") or "")))
    doc["items"] = items[:MAX_POOL_ITEMS]
    doc["generated_at"] = doc.get("generated_at") or now_s
    doc["updated_at"] = now_s
    doc["ttl_h"] = TTL_H
    if not OUT.exists() or doc.get("items"):
        OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return added, skipped


def show() -> int:
    if not OUT.exists():
        print("池子还没生成")
        return 1
    d = json.loads(OUT.read_text(encoding="utf-8-sig"))
    print(f"生成于 {d.get('generated_at')}  TTL={d.get('ttl_h')}h  源={d.get('sources')}  丢弃={d.get('dropped')}")
    for i, it in enumerate(d.get("items") or [], 1):
        print(f"  {i}. [p{it.get('priority')}][{it.get('curiosity_cat')}] {it.get('keyword')}  ({it.get('source')})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--add", nargs="+", default=None, help="追加外部好奇候选（AI 刷视频看到的主题）")
    ap.add_argument("--cat", default=None, help="指定分类：前沿科技/现有技术/热点话题/政治经济/黑话和梗")
    ap.add_argument("--url", default="", help="来源链接")
    ap.add_argument("--source", default="B站观看", help="来源名，默认 B站观看")
    a = ap.parse_args()
    if a.show:
        return show()
    if a.add:
        added, skipped = add_items(a.add, cat=a.cat, url=a.url, source=a.source)
        for it in added:
            print(f"+ [p{it['priority']}][{it['curiosity_cat']}] {it['keyword']} → {it['id']}")
        for s in skipped:
            print(f"- 跳过 {s}")
        print(f"新增 {len(added)} 条 / 跳过 {len(skipped)} 条，池子现在 {len(json.loads(OUT.read_text(encoding='utf-8-sig')).get('items') or [])} 条")
        return 0 if added or skipped else 1
    doc = build(dry=a.dry_run)
    print(f"源={doc['sources']} 丢弃={doc['dropped']} 入池={len(doc['items'])}")
    for it in doc["items"]:
        print(f"  [p{it['priority']}][{it['curiosity_cat']}] {it['keyword']}")
    if a.dry_run:
        print("（dry-run，未落盘）")
    else:
        print(f"已写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
