# -*- coding: utf-8 -*-
"""信息面收数据（2026-11-17 主人要求：先收着，他一个个手工标注）。

和 curiosity.py 的分工：
  curiosity.py  = 抓完直接进决策池，会立刻影响我下一轮干什么。
  info_pool.py  = 只落盘、不进池。主人标了「有用」的条目才值得进 curiosity。

源来自 source_probe.py 实测活着的那批（2026-11-17 实测 24/47），按类别分：
  中文热榜（贴吧/IT之家/Solidot）、科技媒体（Ars/TechCrunch/MIT TR/少数派/量子位）、
  科研顶刊（Nature/Science/PNAS/Lancet/NEJM/JAMA/Cell/bioRxiv/medRxiv/Phys.org/ScienceDaily）、
  实验室（OpenAI/MSR）、GitHub Trending。
X/小红书/IG/FB/Discord/抖音 需登录或签名，脚本抓不到，只在 report 里标出来。

跑法:
  python info_pool.py              # 抓一轮，落 self/info/inbox_YYYYMMDD.jsonl
  python info_pool.py --show 20    # 看最近 20 条（带编号，方便主人标注）
  python info_pool.py --mark 3 good   # 把第 3 条标成 good（bad/skip 同理），写 labels.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
INFO = SELF / "info"
INBOX = INFO  # 每次按天一个文件
LABELS = INFO / "labels.jsonl"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# (名字, 分类, url, 每次取几条)
FEEDS = [
    ("贴吧热议", "hot", "https://tieba.baidu.com/hottopic/browse/topicList", 8),
    ("IT之家", "news", "https://www.ithome.com/rss/", 6),
    ("Solidot", "news", "https://www.solidot.org/index.rss", 6),
    ("量子位", "news", "https://www.qbitai.com/feed", 6),
    ("少数派", "news", "https://sspai.com/feed", 5),
    ("ArsTechnica", "news", "https://feeds.arstechnica.com/arstechnica/index", 5),
    ("TechCrunch", "news", "https://techcrunch.com/feed/", 5),
    ("MIT TechReview", "news", "https://www.technologyreview.com/feed/", 5),
    ("GitHub Trending", "tech",
     "https://api.github.com/search/repositories?q=created:%3E{d}&sort=stars&order=desc&per_page=8", 8),
    ("arXiv cs.CL", "sci",
     "http://export.arxiv.org/api/query?search_query=cat:cs.CL&sortBy=submittedDate&sortOrder=descending&max_results=8", 8),
    ("Nature", "sci", "https://www.nature.com/nature.rss", 6),
    ("Science", "sci", "https://www.science.org/rss/news_current.xml", 6),
    ("PNAS", "sci", "https://www.pnas.org/action/showFeed?type=etoc&feed=rss&jc=pnas", 5),
    ("Lancet", "sci", "https://www.thelancet.com/rssfeed/lancet_current.xml", 5),
    ("NEJM", "sci", "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss", 5),
    ("JAMA", "sci", "https://jamanetwork.com/rss/site_3/67.xml", 5),
    ("Cell", "sci", "https://www.cell.com/cell/current.rss", 5),
    ("bioRxiv", "sci", "https://connect.biorxiv.org/biorxiv_xml.php?subject=all", 5),
    ("medRxiv", "sci", "https://connect.medrxiv.org/medrxiv_xml.php?subject=all", 5),
    ("Phys.org", "sci", "https://phys.org/rss-feed/", 5),
    ("ScienceDaily", "sci", "https://www.sciencedaily.com/rss/all.xml", 5),
    ("IEEE Spectrum", "sci", "https://spectrum.ieee.org/feeds/feed.rss", 5),
    ("OpenAI", "lab", "https://openai.com/news/rss.xml", 5),
    ("MSR", "lab", "https://www.microsoft.com/en-us/research/feed/", 5),
]

# 要给主人看的"抓不到"清单，别装作抓到了
BLOCKED = ["X/Twitter(API key)", "小红书(登录+签名)", "Instagram(Graph token)",
           "Facebook(Graph token)", "Discord(bot token)", "抖音(webmssdk)"]

TAG_RE = re.compile(r"<[^>]+>")
ITEM_RE = re.compile(r"<(item|entry)\b.*?</\1>", re.I | re.S)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
LINK_RE = re.compile(r"<link[^>]*?href=\"([^\"]+)\"|<link[^>]*>(.*?)</link>", re.I | re.S)
DATE_RE = re.compile(r"<(pubDate|published|updated|dc:date)[^>]*>(.*?)</\1>", re.I | re.S)


def clean(s: str) -> str:
    s = s or ""
    # CDATA 必须先去壳：不然 TAG_RE 会把 <![CDATA[标题]]> 整段当标签吃掉（2026-11-17 踩过）
    m = re.match(r"\s*<!\[CDATA\[(.*?)\]\]>\s*$", s, re.S)
    if m:
        s = m.group(1)
    s = html.unescape(TAG_RE.sub("", s))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def get(url: str, timeout: float = 8.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(400_000)


def parse_feed(txt: str, limit: int) -> list[dict]:
    out = []
    for m in ITEM_RE.finditer(txt):
        body = m.group(0)
        t = TITLE_RE.search(body)
        if not t:
            continue
        title = clean(t.group(1))
        if not title or len(title) < 4:
            continue
        lk = LINK_RE.search(body)
        link = clean((lk.group(1) or lk.group(2)) if lk else "")
        d = DATE_RE.search(body)
        out.append({"title": title[:180], "url": link[:300], "date": clean(d.group(2))[:32] if d else ""})
        if len(out) >= limit:
            break
    return out


def parse_json(name: str, raw: bytes, limit: int) -> list[dict]:
    d = json.loads(raw.decode("utf-8", "ignore"))
    out: list[dict] = []
    if "github" in name.lower():
        for it in (d.get("items") or [])[:limit]:
            out.append({"title": f"{it.get('full_name')} ★{it.get('stargazers_count')} — {(it.get('description') or '')[:100]}",
                        "url": it.get("html_url", ""), "date": (it.get("created_at") or "")[:10]})
    elif "贴吧" in name:
        arr = (((d.get("data") or {}).get("bang_topic") or {}).get("topic_list")) or []
        for it in arr[:limit]:
            out.append({"title": clean(it.get("topic_name", "")), "url": it.get("topic_url", ""),
                        "date": it.get("create_time", "")})
    return out


def probe_one(item) -> tuple[str, str, list[dict], str]:
    name, kind, url, limit = item
    if "{d}" in url:
        url = url.replace("{d}", (datetime.now()).strftime("%Y-%m-%d"))
    try:
        raw = get(url)
        txt = raw.decode("utf-8", "ignore")
        rows = parse_json(name, raw, limit) if txt.lstrip()[:1] in "[{" else parse_feed(txt, limit)
        if not rows:
            rows = parse_feed(txt, limit) if txt.lstrip()[:1] in "[{" else []
        return name, kind, rows, "" if rows else "解析到 0 条"
    except urllib.error.HTTPError as e:
        return name, kind, [], f"HTTP {e.code}"
    except Exception as e:
        return name, kind, [], f"{type(e).__name__}: {str(e)[:60]}"


def collect() -> dict:
    INFO.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    path = INBOX / f"inbox_{day}.jsonl"
    seen = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line).get("hash"))
            except Exception:
                pass
    stats, errs, added = {}, [], 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(probe_one, FEEDS))
    with path.open("a", encoding="utf-8") as fh:
        for name, kind, rows, err in results:
            if err:
                errs.append(f"{name}: {err}")
            stats[name] = len(rows)
            for r in rows:
                h = hashlib.sha1((r["title"] + r["url"]).encode("utf-8")).hexdigest()[:12]
                if h in seen:
                    continue
                seen.add(h)
                fh.write(json.dumps({"hash": h, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                     "source": name, "kind": kind, **r}, ensure_ascii=False) + "\n")
                added += 1
    return {"day": day, "path": str(path), "added": added, "per_source": stats, "errors": errs}


def tail(n: int = 20) -> list[dict]:
    files = sorted(INBOX.glob("inbox_*.jsonl"))
    if not files:
        return []
    rows = []
    for f in files[::-1]:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        if len(rows) >= n:
            break
    return rows[-n:]


def mark(idx: int, verdict: str) -> str:
    rows = tail(200)
    if not rows:
        return "inbox 是空的，先跑一次 collect"
    if not (1 <= idx <= len(rows)):
        return f"编号超范围（1..{len(rows)}）"
    r = rows[idx - 1]
    INFO.mkdir(parents=True, exist_ok=True)
    with LABELS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "verdict": verdict, **r}, ensure_ascii=False) + "\n")
    return f"已标 {verdict}：{r['title'][:60]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", nargs="?", type=int, const=20, default=None)
    ap.add_argument("--mark", nargs=2, metavar=("IDX", "VERDICT"), default=None)
    a = ap.parse_args()
    if a.mark:
        print(mark(int(a.mark[0]), a.mark[1]))
        return 0
    if a.show is not None:
        rows = tail(a.show)
        for i, r in enumerate(rows, 1):
            print(f"{i:>3}. [{r['kind']}/{r['source']}] {r['title'][:88]}")
        print(f"\n共 {len(rows)} 条；给主人标注用 --mark 编号 good|bad|skip")
        return 0
    st = collect()
    print(f"新增 {st['added']} 条 -> {st['path']}")
    for k, v in st["per_source"].items():
        if v:
            print(f"  {k:<16}{v:>3}")
    if st["errors"]:
        print("抓不到：" + "；".join(st["errors"]))
    print("需凭证（脚本抓不到）：" + "，".join(BLOCKED))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
