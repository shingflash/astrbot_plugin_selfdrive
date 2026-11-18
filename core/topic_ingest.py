# -*- coding: utf-8 -*-
"""采集层（2026-11-17）：把多源热点全量灌进 topic.db，供各种任务共用。

主人口径：
  · 403 / 要 cookie 的源一律不碰（X、小红书、微博、知乎、抖音、IG、FB、Discord…）
  · 采集嵌进现有任务跑，不单独起进程；本文件既能被 import，也能单独跑
  · 去重交给 topic_store（硬指纹 + 3-gram 近重复），这里只负责拿数据

用法：
  python self/topic_ingest.py            # 跑一轮全量采集
  python self/topic_ingest.py --dry      # 只看每个源能拿几条，不写库
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
if str(SELF) not in sys.path:
    sys.path.insert(0, str(SELF))

import info_pool as ip          # 复用它的 get/parse/clean，别写第二套
import topic_store as ts

# ---- 明确不碰的源：要么 403/401，要么必须登录凭证 ----
# 2026-11-17 实测：微博 403、知乎 401、抖音 200 但 0 条、Tom's 403、ACM 403、
# OpenReview 403、Semantic Scholar 429、小红书要 x-s 签名、X/IG/FB/Discord 要 token。
UNSUPPORTED = [
    ("X / Twitter", "需 API key 或 cookies"),
    ("小红书", "需登录 cookie，签名接口 x-s 无法离线复现"),
    ("微博热搜", "403"),
    ("知乎热榜", "401"),
    ("抖音热榜", "200 但 0 条，需 webmssdk"),
    ("Instagram", "需 Graph API token"),
    ("Facebook", "需 Graph API token"),
    ("Discord", "需 bot token"),
    ("Tom's Hardware", "403"),
    ("ACM TechNews", "403"),
    ("OpenReview", "403"),
    ("BMJ", "404，RSS 路径已失效"),
    ("Semantic Scholar", "429 限频，无 key 不碰"),
]

# 在 info_pool.FEEDS 基础上补/修
FIXUPS = {
    # arxiv 老被 http 卡超时，换 https
    "arXiv cs.CL": ("sci",
                    "https://export.arxiv.org/api/query?search_query=cat:cs.CL&sortBy=submittedDate"
                    "&sortOrder=descending&max_results=8", 8),
    # GitHub 原来查"今天之后创建"，等于永远 0 条；改成近 30 天
    "GitHub Trending": ("tech",
                        "https://api.github.com/search/repositories?q=created:%3E{d30}"
                        "&sort=stars&order=desc&per_page=10", 10),
}
EXTRA = [
    # HackerNews：firebase 接口返回裸 id 数组解析不了，ycombinator.com/rss 国内超时，
    # 用 hnrss 镜像（带标题，实测稳）
    ("HackerNews", "tech", "https://hnrss.org/frontpage", 10),
]


def feeds() -> list[tuple]:
    out = []
    for name, kind, url, limit in ip.FEEDS:
        if name in FIXUPS:
            k, u, lim = FIXUPS[name]
            out.append((name, k, u, lim))
        else:
            out.append((name, kind, url, limit))
    out.extend(EXTRA)
    return out


def _adapter(name: str, url: str) -> str:
    if "api.github.com" in url:
        return "github"
    if "arxiv.org" in url:
        return "arxiv"
    if "firebaseio.com" in url:
        return "hn"
    if "tieba.baidu.com" in url:
        return "tieba"
    return "rss"


INGEST_STAMP = SELF / ".topic_last_ingest"
INGEST_COOLDOWN_MIN = 25.0   # 同一轮任务里别重复抓 25 个源


def maybe_run(cooldown_min: float = INGEST_COOLDOWN_MIN) -> dict:
    """给现有任务调用的入口：距上次采集不到 cooldown 就跳过，返回 skipped。"""
    try:
        last = float(INGEST_STAMP.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        last = 0.0
    if time.time() - last < cooldown_min * 60:
        return {"skipped": True, "reason": f"距上次采集不足 {cooldown_min:g} 分钟"}
    r = run()
    try:
        INGEST_STAMP.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass
    return r


def run(dry: bool = False, workers: int = 8) -> dict:
    rows: list[dict] = []
    per: dict[str, int] = {}
    errs: list[str] = []
    items = feeds()
    t0 = time.time()

    def one(item):
        name, kind, url, limit = item
        u = url
        if "{d30}" in u:
            u = u.replace("{d30}", (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
        if "{d}" in u:
            u = u.replace("{d}", datetime.now().strftime("%Y-%m-%d"))
        n, k, got, err = ip.probe_one((name, kind, u, limit))
        return name, k, u, got, err

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, items))

    for name, k, u, got, err in results:
        per[name] = len(got)
        if err:
            errs.append(f"{name}: {err}")
        for r in got:
            rows.append({"source": name, "kind": k, "title": r.get("title", ""),
                         "url": r.get("url", ""), "date": r.get("date", "")})
        if not dry:
            ts.upsert_source(name, k, u, _adapter(name, u), ok=not err, err=err)

    # 不支持的源也登记成 disabled，免得以后又有人去撞
    if not dry:
        for name, why in UNSUPPORTED:
            ts.upsert_source(name, "unsupported", "", "none", ok=False, err=why)
            with ts._conn() as c:
                c.execute("UPDATE source SET enabled=0 WHERE name=?", (name,))

    res = {"ms": int((time.time() - t0) * 1000), "sources": len(items),
           "rows": len(rows), "per_source": per, "errors": errs}
    if not dry:
        res["store"] = ts.ingest(rows)
        res["stats"] = ts.stats()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="只输出一行 JSON，给定时任务用")
    a = ap.parse_args()
    r = run(dry=a.dry)
    if a.quiet:
        slim = {k: r[k] for k in ("ms", "sources", "rows", "store", "errors") if k in r}
        slim["ok_src"] = sum(1 for v in r["per_source"].values() if v > 0)
        print(json.dumps(slim, ensure_ascii=False))
        return 0
    print(f"[采集] {r['sources']} 个源 / {r['rows']} 条 / {r['ms']}ms")
    dead = [n for n, v in r["per_source"].items() if v == 0]
    print("  拿到数据: %d 个源" % sum(1 for v in r["per_source"].values() if v > 0))
    if dead:
        print("  空手: %s" % ", ".join(dead))
    if r["errors"]:
        print("  报错: %s" % "; ".join(r["errors"][:6]))
    if "store" in r:
        s = r["store"]
        print(f"[入库] 新增 {s['added']} / 重复 {s['dup']} / 近重复 {s['near_dup']} / 新候选 {s['topics']}")
        st = r["stats"]
        print(f"[库] 待用 {st['new']} 已用 {st['used']} 丢弃 {st['dropped']} 过期 {st['expired']}"
              f" | 全量 {st['raw']} 条 {st['db_kb']}KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
