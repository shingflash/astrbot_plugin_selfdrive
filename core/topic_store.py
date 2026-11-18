# -*- coding: utf-8 -*-
"""题目总库（2026-11-17 主人派活）：多源热点全量落库 → 筛成候选 → 用完剔除。

主人的三条口径：
  1) 403 / 要 cookie 的源全部摘掉，不在源表里反复撞墙
  2) 库放 self/topic.db
  3) 采集嵌进现有任务，写一个共享池给别的任务用；去重，用过的剔除

分工：
  raw_item  全量原样存档，永不覆盖，只做增量（这是"数据库"）
  topic     从 raw_item 筛出来、够格当题目的候选（status=new 才能出）
  consume   哪条被哪个轮在哪天吃掉、结果怎样（用完剔除的依据）
  source    源注册表 + 健康度（fail_n 连续失败就自动降频）

用法：
  python self/topic_store.py --stats
  python self/topic_store.py --pick 6            # 取 6 条未用的候选（JSON）
  python self/topic_store.py --used <key>        # 标成已用，之后不再出
  python self/topic_store.py --drop <key> 理由
  python self/topic_store.py --prune 60          # 清 60 天前的 raw_item 以外的垃圾
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os  # SELFDRIVE_HOME
HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
DB = SELF / "topic.db"
CST = timezone(timedelta(hours=8))
TTL_H = 48.0          # 热点 48h 就凉，过期不再出题
NEAR_DUP = 0.82       # 3-gram Jaccard 超过这个当同一条

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
  name TEXT PRIMARY KEY,
  kind TEXT,
  url TEXT,
  adapter TEXT,
  enabled INTEGER DEFAULT 1,
  ok_n INTEGER DEFAULT 0,
  fail_n INTEGER DEFAULT 0,
  last_ts TEXT,
  last_err TEXT
);
CREATE TABLE IF NOT EXISTS raw_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dkey TEXT UNIQUE,          -- 归一化标题指纹，硬去重
  source TEXT,
  kind TEXT,
  title TEXT,
  url TEXT,
  ts_pub TEXT,
  fetched_at TEXT,
  ngram TEXT,                -- 3-gram 签名，近重复比对用
  raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_raw_fetch ON raw_item(fetched_at);
CREATE INDEX IF NOT EXISTS ix_raw_source ON raw_item(source, fetched_at);
CREATE TABLE IF NOT EXISTS topic (
  dkey TEXT PRIMARY KEY,
  title TEXT,
  url TEXT,
  source TEXT,
  kind TEXT,
  cat TEXT,
  pri INTEGER DEFAULT 0,
  score REAL DEFAULT 0,
  why TEXT,
  first_seen TEXT,
  status TEXT DEFAULT 'new',   -- new / used / dropped / expired
  used_at TEXT,
  n_src INTEGER DEFAULT 1      -- 跨源共振：同一件事被几个源同时报到
);
CREATE INDEX IF NOT EXISTS ix_topic_status ON topic(status, score);
CREATE TABLE IF NOT EXISTS consume (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dkey TEXT,
  ts TEXT,
  verdict TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS ix_consume_dkey ON consume(dkey);
"""


def now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)
        # 老库补列（2026-11-17 加 n_src）
        try:
            c.execute("ALTER TABLE topic ADD COLUMN n_src INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass


# ---------------- 归一化 / 去重 ----------------

_PUNCT = re.compile(r"[\s\u3000,，.。!！?？:：;；'\"“”‘’()（）\[\]【】<>《》|/\\\-—~·、*#@$%^&+=_]+")
_NOISE_SUFFIX = re.compile(r"(\s*[-|—_]\s*[^-|—_]{0,20})$")


def norm_title(t: str) -> str:
    """归一化：全角转半角、去标点空白、小写。用来做硬指纹。"""
    t = unicodedata.normalize("NFKC", t or "")
    t = _PUNCT.sub("", t)
    return t.strip().lower()


def dkey_of(title: str, url: str = "") -> str:
    n = norm_title(title)
    if not n:
        n = (url or "").strip()
    return hashlib.sha1(n.encode("utf-8")).hexdigest()[:16]


def ngram_of(title: str) -> str:
    """3-gram 集合，用 | 分隔存（不能直接拼字符串，否则 jaccard 退化成字符集比对，
    中文标题会被误判成大面积近重复——2026-11-17 踩过：136 条误杀 49 条）。"""
    n = norm_title(title)
    if len(n) < 3:
        return n
    return "|".join(sorted({n[i:i + 3] for i in range(len(n) - 2)}))


def jaccard(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa = set(a.split("|")) if "|" in a or len(a) > 3 else {a}
    sb = set(b.split("|")) if "|" in b or len(b) > 3 else {b}
    return len(sa & sb) / float(len(sa | sb) or 1)


# ---------------- 写入 ----------------

def ingest(rows: list[dict], kind: str = "") -> dict:
    """rows: [{title,url,date,source,kind}]，全量落 raw_item，新的同时进 topic。

    返回 {added, dup, near_dup, topics}
    """
    init()
    added = dup = near = 0
    made = 0
    seen_ng: list[str] = []
    with _conn() as c:
        # 近重复只在最近 3000 条里比，够用且不慢
        for r in c.execute(
                "SELECT ngram FROM raw_item WHERE fetched_at > ? LIMIT 3000",
                ((datetime.now(CST) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),)):
            if r["ngram"]:
                seen_ng.append(r["ngram"])
        for it in rows:
            title = (it.get("title") or "").strip()
            if len(title) < 4:
                continue
            url = (it.get("url") or "").strip()
            dk = dkey_of(title, url)
            ng = ngram_of(title)
            if c.execute("SELECT 1 FROM raw_item WHERE dkey=? AND source=?",
                        (dk, it.get("source", ""))).fetchone():
                dup += 1
                continue
            if c.execute("SELECT 1 FROM raw_item WHERE dkey=?", (dk,)).fetchone():
                dup += 1
                # 同一件事被**别的源**也报了 → 共振计数 +1（跨源共振是最值钱的信号）
                # 注意必须排除同源重复抓取，否则贴吧抓两次就变成"2源共振"了
                c.execute("UPDATE topic SET n_src=n_src+1 WHERE dkey=?", (dk,))
                continue
            if any(jaccard(ng, x) >= NEAR_DUP for x in seen_ng[-800:]):
                near += 1
                continue
            k = it.get("kind") or kind
            c.execute(
                "INSERT OR IGNORE INTO raw_item(dkey,source,kind,title,url,ts_pub,fetched_at,ngram,raw)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (dk, it.get("source", ""), k, title[:200], url[:400],
                 it.get("date", ""), now(), ng, json.dumps(it, ensure_ascii=False)[:2000]))
            added += 1
            seen_ng.append(ng)
            # 所有源都进候选池，由上层分类器决定要不要当题目（别在存储层替上层做判断）
            cur = c.execute(
                "INSERT OR IGNORE INTO topic(dkey,title,url,source,kind,first_seen,status)"
                " VALUES(?,?,?,?,?,?, 'new')",
                (dk, title[:200], url[:400], it.get("source", ""), k, now()))
            if cur.rowcount:
                made += 1
    return {"added": added, "dup": dup, "near_dup": near, "topics": made}


def upsert_source(name: str, kind: str, url: str, adapter: str,
                  ok: bool, err: str = "") -> None:
    init()
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO source(name,kind,url,adapter) VALUES(?,?,?,?)",
                  (name, kind, url[:500], adapter))
        if ok:
            c.execute("UPDATE source SET ok_n=ok_n+1, fail_n=0, last_ts=?, last_err='' WHERE name=?",
                      (now(), name))
        else:
            c.execute("UPDATE source SET fail_n=fail_n+1, last_ts=?, last_err=? WHERE name=?",
                      (now(), err[:120], name))


def add_topic(title: str, url: str = "", source: str = "手动", kind: str = "manual",
              cat: str = "", pri: int = 0, why: str = "") -> str:
    """手工/外部任务往池里塞一条，返回 dkey。"""
    init()
    dk = dkey_of(title, url)
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO raw_item(dkey,source,kind,title,url,fetched_at,ngram,raw)"
                  " VALUES(?,?,?,?,?,?,?,'{}')",
                  (dk, source, kind, title[:200], url[:400], now(), ngram_of(title)))
        c.execute("INSERT OR IGNORE INTO topic(dkey,title,url,source,kind,cat,pri,why,first_seen,status)"
                  " VALUES(?,?,?,?,?,?,?,?,?, 'new')",
                  (dk, title[:200], url[:400], source, kind, cat, pri, why, now()))
    return dk


# ---------------- 取题 / 剔除 ----------------

def pick(n: int = 6, kinds: tuple = ("hot",), ttl_h: float = TTL_H) -> list[dict]:
    """取 n 条 status=new 且没过期的候选。按 pri、score 排。"""
    init()
    cutoff = (datetime.now(CST) - timedelta(hours=ttl_h)).strftime("%Y-%m-%d %H:%M:%S")
    q = ("SELECT dkey,title,url,source,kind,cat,pri,score,why,first_seen,n_src FROM topic"
         " WHERE status='new' AND first_seen>=?")
    args: list = [cutoff]
    if kinds:
        q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args += list(kinds)
    # 排序：分类优先级 > 跨源共振 > 手填分 > 谁先来
    q += " ORDER BY (pri*10 + n_src*6 + score) DESC, first_seen ASC LIMIT ?"
    args.append(n)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def mark_used(dkeys: list[str], verdict: str = "used", note: str = "") -> int:
    """用完即剔除：置 used + 记 consume，之后 pick 不会再给。"""
    init()
    t = now()
    n = 0
    with _conn() as c:
        for dk in dkeys:
            cur = c.execute("UPDATE topic SET status='used', used_at=? WHERE dkey=? AND status='new'", (t, dk))
            n += cur.rowcount
            c.execute("INSERT INTO consume(dkey,ts,verdict,note) VALUES(?,?,?,?)",
                      (dk, t, verdict, note[:200]))
    return n


def mark_dropped(dkeys: list[str], reason: str = "") -> int:
    init()
    t = now()
    n = 0
    with _conn() as c:
        for dk in dkeys:
            n += c.execute("UPDATE topic SET status='dropped', used_at=? WHERE dkey=? AND status='new'",
                           (t, dk)).rowcount
            c.execute("INSERT INTO consume(dkey,ts,verdict,note) VALUES(?,?,'dropped',?)",
                      (dk, t, reason[:200]))
    return n


def expire(ttl_h: float = TTL_H) -> int:
    init()
    cutoff = (datetime.now(CST) - timedelta(hours=ttl_h)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        return c.execute("UPDATE topic SET status='expired' WHERE status='new' AND first_seen<?",
                         (cutoff,)).rowcount


def prune(days: int = 60) -> dict:
    """raw_item 保留全量档案（这是数据库），只清掉被 consume 过的 topic 残渣和超老 raw。"""
    init()
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        t = c.execute("DELETE FROM topic WHERE status IN ('used','dropped','expired') AND used_at<?",
                      (cutoff,)).rowcount
        r = c.execute("DELETE FROM raw_item WHERE fetched_at<? AND dkey NOT IN (SELECT dkey FROM topic)",
                      (cutoff,)).rowcount
    return {"topic_cleaned": t, "raw_cleaned": r}


def sync_from_decisions(limit_files: int = 400, iter_out_dir=None) -> int:
    """扫 iter_out/decision_*.json，把真被吃掉的候选标 used。

    iter_cycle 自己有一套 consumed 扫描（按 id / 按主题），这里补的是库这一侧：
    不标的话 status 永远是 new，pick 每轮都出同一条。
    返回这轮新标掉几条。
    """
    init()
    d = Path(iter_out_dir) if iter_out_dir else (SELF / "iter_out")
    if not d.exists():
        return 0
    keys: set[str] = set()
    for f in sorted(d.glob("decision_*.json"))[-limit_files:]:
        try:
            j = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        p = j.get("pick") or {}
        if p.get("kind") not in ("curiosity", "inspiration"):
            continue
        for k in (p.get("dkey"), p.get("keyword"), p.get("title")):
            if not k:
                continue
            keys.add(dkey_of(str(k)))
            if len(str(k)) >= 4:
                keys.add(str(k))
    if not keys:
        return 0
    t = now()
    n = 0
    with _conn() as c:
        for k in keys:
            cur = c.execute(
                "UPDATE topic SET status='used', used_at=? WHERE status='new' AND (dkey=? OR dkey=?)",
                (t, k, dkey_of(k)))
            if cur.rowcount:
                n += cur.rowcount
                c.execute("INSERT INTO consume(dkey,ts,verdict,note) VALUES(?,?,'picked','扫决策文件补标')",
                          (k, t))
    return n


def stats() -> dict:
    init()
    with _conn() as c:
        def one(sql, args=()):
            return c.execute(sql, args).fetchone()[0]
        st = {k: one(f"SELECT COUNT(*) FROM topic WHERE status='{k}'")
              for k in ("new", "used", "dropped", "expired")}
        st["raw"] = one("SELECT COUNT(*) FROM raw_item")
        st["raw_today"] = one("SELECT COUNT(*) FROM raw_item WHERE fetched_at>=?",
                              (datetime.now(CST).strftime("%Y-%m-%d") + " 00:00:00",))
        st["consume"] = one("SELECT COUNT(*) FROM consume")
        st["db_kb"] = round(DB.stat().st_size / 1024, 1) if DB.exists() else 0
        st["by_source"] = {r[0]: r[1] for r in c.execute(
            "SELECT source,COUNT(*) FROM raw_item GROUP BY source ORDER BY 2 DESC LIMIT 15")}
        st["by_kind"] = {r[0]: r[1] for r in c.execute(
            "SELECT kind,COUNT(*) FROM raw_item GROUP BY kind")}
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--pick", type=int, default=0)
    ap.add_argument("--kinds", default="hot", help="逗号分隔，留空=不限")
    ap.add_argument("--used", nargs="*", default=None)
    ap.add_argument("--drop", nargs=2, default=None, metavar=("KEY", "理由"))
    ap.add_argument("--expire", action="store_true")
    ap.add_argument("--prune", type=int, default=0)
    a = ap.parse_args()

    if a.stats:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    if a.pick:
        ks = tuple(x for x in a.kinds.split(",") if x) if a.kinds else ()
        rows = pick(a.pick, ks)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    if a.used is not None:
        print("已剔除 %d 条" % mark_used(a.used))
    if a.drop:
        print("已丢弃 %d 条：%s" % (mark_dropped([a.drop[0]], a.drop[1]), a.drop[1]))
    if a.expire:
        print("过期 %d 条" % expire())
    if a.prune:
        print(json.dumps(prune(a.prune), ensure_ascii=False))
    if not any([a.stats, a.pick, a.used is not None, a.drop, a.expire, a.prune]):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
