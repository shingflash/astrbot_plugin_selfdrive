# -*- coding: utf-8 -*-
"""信息源探针（2026-11-17 主人要求扩信息面，先收数据不落地）。

目的：在把源接进 curiosity.py 之前，先如实测一遍「哪些能匿名拿到」。
规则：
  1. 只发一次 GET，6 秒超时，不重试、不带 cookie、不登录 —— 拿不到就算拿不到。
  2. 判定「能用」= HTTP 200 且 body 里能找到至少 3 条候选标题（粗暴但足够）。
  3. 需要登录/token 的平台（X/IG/FB/Discord/小红书）只登记成 need_cred，不假装能抓。
  4. 输出 self/source_probe.json，供主人勾选后再接。

跑法: python source_probe.py
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
OUT = SELF / "source_probe.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# name, url, kind, 期望的标题字段痕迹（正则，用于判断"真拿到内容了"）
SOURCES = [
    # ---- 中文社交热榜 ----
    ("微博热搜", "https://weibo.com/ajax/side/hotSearch", "hot", r'"word"|"note"'),
    ("知乎热榜", "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=20", "hot", r'"title"'),
    ("抖音热榜", "https://www.douyin.com/aweme/v1/web/hot/search/list/", "hot", r'"word"|"sentence"'),
    ("贴吧热议", "https://tieba.baidu.com/hottopic/browse/topicList", "hot", r'"topic_name"'),
    # ---- 科技/开发者 ----
    ("HackerNews", "https://hacker-news.firebaseio.com/v0/topstories.json", "tech", r'^\['),
    ("GitHub Trending", "https://api.github.com/search/repositories?q=created:%3E2026-10-20&sort=stars&order=desc&per_page=10", "tech", r'"full_name"'),
    ("Reddit LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/top.json?t=day&limit=10", "tech", r'"title"'),
    # ---- 中文媒体 RSS ----
    ("36氪", "https://36kr.com/feed", "news", r"<title>"),
    ("机器之心", "https://www.jiqizhixin.com/rss", "news", r"<title>"),
    ("量子位", "https://www.qbitai.com/feed", "news", r"<title>"),
    ("少数派", "https://sspai.com/feed", "news", r"<title>"),
    ("Solidot", "https://www.solidot.org/index.rss", "news", r"<title>"),
    ("IT之家", "https://www.ithome.com/rss/", "news", r"<title>"),
    # ---- 英文非政治科技媒体 ----
    ("TheVerge", "https://www.theverge.com/rss/index.xml", "news", r"<title>"),
    ("ArsTechnica", "https://feeds.arstechnica.com/arstechnica/index", "news", r"<title>"),
    ("MIT TechReview", "https://www.technologyreview.com/feed/", "news", r"<title>"),
    ("TechCrunch", "https://techcrunch.com/feed/", "news", r"<title>"),
    ("AnandTech", "https://www.anandtech.com/rss/", "news", r"<title>"),
    ("Tom's Hardware", "https://www.tomshardware.com/feeds/all", "news", r"<title>"),
    # ---- 前沿科研 ----
    ("arXiv cs.AI", "http://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=10", "sci", r"<title>"),
    ("arXiv cs.CL", "http://export.arxiv.org/api/query?search_query=cat:cs.CL&sortBy=submittedDate&sortOrder=descending&max_results=10", "sci", r"<title>"),
    ("Nature", "https://www.nature.com/nature.rss", "sci", r"<title>"),
    ("Science News", "https://www.science.org/rss/news_current.xml", "sci", r"<title>"),
    ("PNAS", "https://www.pnas.org/action/showFeed?type=etoc&feed=rss&jc=pnas", "sci", r"<title>"),
    ("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml", "sci", r"<title>"),
    ("Phys.org", "https://phys.org/rss-feed/", "sci", r"<title>"),
    ("Nature Briefing", "https://www.nature.com/nature/articles?type=news&error=cookies_not_supported&code=1", "sci", r"<title>"),
    ("bioRxiv", "https://connect.biorxiv.org/biorxiv_xml.php?subject=all", "sci", r"<title>"),
    ("medRxiv", "https://connect.medrxiv.org/medrxiv_xml.php?subject=all", "sci", r"<title>"),
    ("The Lancet", "https://www.thelancet.com/rssfeed/lancet_current.xml", "sci", r"<title>"),
    ("NEJM", "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss", "sci", r"<title>"),
    ("BMJ", "https://www.bmj.com/rss/recent", "sci", r"<title>"),
    ("JAMA", "https://jamanetwork.com/rss/site_3/67.xml", "sci", r"<title>"),
    ("Cell", "https://www.cell.com/cell/current.rss", "sci", r"<title>"),
    ("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss", "sci", r"<title>"),
    ("ACM TechNews", "https://technews.acm.org/rss.cfm", "sci", r"<title>"),
    ("Semantic Scholar", "https://api.semanticscholar.org/graph/v1/paper/search?query=large+language+model&limit=10&fields=title,year,venue", "sci", r'"title"'),
    ("PapersWithCode", "https://paperswithcode.com/api/v1/papers/?items_per_page=10", "sci", r'"title"'),
    ("OpenReview(ICLR)", "https://api2.openreview.net/notes?content.venue=ICLR%202025&limit=10", "sci", r'"title"'),
    ("DOAJ", "https://doaj.org/api/search/articles/AI?pageSize=10", "sci", r'"title"'),
    ("PubMed(AI)", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=artificial+intelligence&retmax=10&retmode=json", "sci", r'"idlist"'),
    # ---- 前沿公司/实验室博客 ----
    ("OpenAI", "https://openai.com/news/rss.xml", "lab", r"<title>"),
    ("DeepMind", "https://deepmind.google/blog/rss.xml", "lab", r"<title>"),
    ("Anthropic", "https://www.anthropic.com/news", "lab", r"<title>"),
    ("HuggingFace Blog", "https://huggingface.co/blog/feed.xml", "lab", r"<title>"),
    ("Google Research", "https://research.google/blog/rss/", "lab", r"<title>"),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/", "lab", r"<title>"),
]

# 需要凭证、脚本抓不到的平台：只登记，不给假结果
NEED_CRED = [
    ("X / Twitter", "需 API key 或 cookies；公开 nitter 节点基本已死"),
    ("小红书", "需登录 cookie，签名接口 x-s 无法离线复现"),
    ("Instagram", "需登录/Graph API token"),
    ("Facebook", "需 Graph API token"),
    ("Discord", "需 bot token + 已加入的服务器频道 ID"),
    ("抖音(签名)", "热榜无签名时可能返空，需 webmssdk 或 cookies"),
]


def probe(item) -> dict:
    name, url, kind, pat = item
    t0 = time.time()
    rec = {"name": name, "url": url, "kind": kind}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read(200_000)
            code = getattr(r, "status", 200)
        txt = raw.decode("utf-8", "ignore")
        hits = len(re.findall(pat, txt, re.I | re.M))
        rec.update({"http": code, "bytes": len(raw), "hits": hits,
                    "ok": bool(code == 200 and hits >= 3), "ms": int((time.time() - t0) * 1000)})
        if not rec["ok"]:
            rec["reason"] = f"200 但只匹配到 {hits} 条，可能被拦或结构变了"
    except urllib.error.HTTPError as e:
        rec.update({"ok": False, "http": e.code, "reason": f"HTTP {e.code}", "ms": int((time.time() - t0) * 1000)})
    except Exception as e:
        rec.update({"ok": False, "reason": f"{type(e).__name__}: {str(e)[:80]}", "ms": int((time.time() - t0) * 1000)})
    return rec


def main() -> int:
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(probe, SOURCES))
    ok = [r for r in rows if r.get("ok")]
    bad = [r for r in rows if not r.get("ok")]
    OUT.write_text(json.dumps({"probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "ok": ok, "failed": bad, "need_cred": NEED_CRED},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"可用 {len(ok)} / 探测 {len(rows)}")
    for r in ok:
        print(f"  OK   {r['name']:<18} {r['kind']:<5} {r['ms']:>5}ms  hits={r.get('hits')}")
    for r in bad:
        print(f"  FAIL {r['name']:<18} {r.get('http', '-')!s:<5} {r.get('reason', '')[:60]}")
    print(f"\n需凭证（等主人给）: {', '.join(n for n, _ in NEED_CRED)}")
    print(f"明细 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
