# -*- coding: utf-8 -*-
"""自省池 —— 从自己留下的记录里推出「接下来该干什么」，而不是等热搜喂候选。

主人 2026-11-17 01:34：「以前你会自己找方向而不是非得拉热搜」。
所以这个池子的货不来自网络，全来自本地文本：
  · memory/DANGLING.md  悬空条目（说了一半没下文的事）
  · memory/FACTS.md     待修/待核/未完成的事实条目
  · alerts.jsonl        还没处理的告警
  · self/changes.md     改了但没写清后续的地方

只读文本、只算哈希，不联网不烧钱。输出 self/goals_introspect.json，
由 iter_cycle.py 在装配候选时读入（kind=introspect，归 explore 池）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
WS = HERE.parent
OUT = HERE / "goals_introspect.json"
TTL_H = 6.0
CST = timezone(timedelta(hours=8))

DANGLE = WS / "memory" / "DANGLING.md"
FACTS = WS / "memory" / "FACTS.md"
ALERTS = WS / "alerts.jsonl"
CHANGES = HERE / "changes.md"

FACT_KEYS = re.compile(r"(待办|待修|待核|待定|未修|未完成|未落地|TODO|悬空|还没)")
MAX_ITEMS = 12


def _now() -> datetime:
    return datetime.now(CST)


def _hid(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def _mk(src: str, title: str, prio: int = 4, cost: float = 0.1,
        tier: str = "t1_readonly") -> dict:
    return {
        "id": "ins_" + _hid(src + "|" + title),
        "title": title,
        "kind": "introspect",
        "cost_rmb": cost,
        "priority": prio,
        "tier": tier,
        "src": src,
    }


def from_dangling() -> list:
    """悬空表格里的活跃行 —— 这是最该续写的东西。"""
    out = []
    if not DANGLE.exists():
        return out
    try:
        txt = DANGLE.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    in_active = False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("## "):
            in_active = "活跃" in s
            continue
        if not in_active:
            continue
        m = re.match(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*([\d\-]+)\s*\|\s*(.*?)\s*\|?$", s)
        if not m:
            continue
        n, what, when, state = m.group(1), m.group(2), m.group(3), m.group(4)
        if "已废" in state or "已办" in state:
            continue
        out.append(_mk(f"DANGLING#{n}", f"续写悬空：{what}（{when} 起，状态：{state[:40]}）",
                       prio=5, cost=0.05))
    return out


def from_facts() -> list:
    """FACTS 里带「待/未/悬」的条目：多半是登记了却没收尾的事。"""
    out = []
    if not FACTS.exists():
        return out
    try:
        txt = FACTS.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    for i, line in enumerate(txt.splitlines(), 1):
        s = line.strip()
        if len(s) < 8 or s.startswith("#") or s.startswith("```"):
            continue
        if not FACT_KEYS.search(s):
            continue
        if "SUPERSEDED" in txt[:0]:
            continue
        body = re.sub(r"^[-*>\d.\s]+", "", s)[:110]
        out.append(_mk(f"FACTS:{i}", f"核对并收尾：{body}", prio=4, cost=0.1))
        if len(out) >= 6:
            break
    return out


def from_alerts() -> list:
    """最近还没消掉的告警：先看是不是还在响。"""
    out = []
    if not ALERTS.exists():
        return out
    try:
        lines = [l for l in ALERTS.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except Exception:
        return out
    seen = set()
    for raw in lines[-40:]:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        lv = str(d.get("level") or d.get("severity") or "").lower()
        if lv not in ("warn", "warning", "error", "crit", "critical"):
            continue
        if d.get("acked") or d.get("resolved"):
            continue
        msg = str(d.get("msg") or d.get("message") or d.get("title") or "")[:110]
        if not msg or msg in seen:
            continue
        seen.add(msg)
        out.append(_mk(f"alert:{d.get('ts', '?')}", f"处理告警：{msg}", prio=5, cost=0.05))
    return out[-3:]


def from_changes() -> list:
    """changes.md 最近几条里写「待」「下一步」的，等于自己给自己留的活。"""
    out = []
    if not CHANGES.exists():
        return out
    try:
        lines = CHANGES.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return out
    for i, line in enumerate(lines[-80:], 1):
        s = line.strip()
        if len(s) < 10 or not FACT_KEYS.search(s):
            continue
        body = re.sub(r"^[-*>\d.\s]+", "", s)[:110]
        out.append(_mk(f"changes:tail{i}", f"接着做：{body}", prio=4, cost=0.1))
        if len(out) >= 3:
            break
    return out


def from_changelog_tail() -> list:
    """CHANGELOG 尾巴上「需重载 / 未生效 / 待请示」的条目 —— 这是我自己留的尾巴。"""
    out = []
    p = WS / "CHANGELOG.md"
    if not p.exists():
        return out
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return out
    pat = re.compile(r"(需重载|未生效|待请示|需口令|待确认)[^。]{0,40}$")
    for i, line in enumerate(lines[-60:], 1):
        s = line.strip()
        if len(s) < 12 or not pat.search(s):
            continue
        m = pat.search(s)
        seg = s[max(s.rfind("|", 0, m.start()) + 1, 0):]
        body = seg.strip(" |-")[:110]
        out.append(_mk(f"CHANGELOG:tail{i}", f"接手自己留的尾巴：{body}", prio=4, cost=0.1))
        if len(out) >= 3:
            break
    return out


def from_meta_switches() -> list:
    """自己机制上的假开关 / 断链（2026-11-17 从优化池并进来）。

    以前这条在 optimize 里算「元」桶；主人说并进自省轮 —— 它本来也不是改代码，
    是「这套机制名存实亡」的自省，放这里才算归位。
    """
    out = []
    try:
        import optimize as _opt
        got = _opt.meta_findings()
    except Exception:
        return out
    for f in got or []:
        it = _mk(f"meta:{f.get('key')}", f"机制自省：{f.get('title', '')}", prio=5, cost=0.05)
        it["evidence"] = f.get("evidence", "")
        it["verify"] = f.get("verify", "")
        it["variants"] = f.get("variants", [])
        out.append(it)
    return out


def from_blocked_decisions() -> list:
    """最近被钱/闸门挡住的事：条件变了就该重做，不是记完就忘。"""
    out = []
    d = HERE / "iter_out"
    if not d.exists():
        return out
    files = sorted(d.glob("decision_*.json"))[-40:]
    seen = set()
    for f in reversed(files):
        try:
            j = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        b = j.get("blocked")
        if not b:
            continue
        p = j.get("pick") or {}
        title = str(p.get("title") or "")
        if not title or title in seen:
            continue
        seen.add(title)
        out.append(_mk(f"blocked:{b}", f"补做上次被拦下的事（{b}）：{title[:80]}", prio=4, cost=0.1))
        if len(out) >= 2:
            break
    return out


def from_episodes(max_days: int = 3) -> list:
    """最近几天原话里「回头 / 下次 / 等我」——答应了没做的那种。"""
    out = []
    d = WS / "memory" / "episodes"
    if not d.exists():
        return out
    pat = re.compile(r"(回头|下次|等我|再说|待会儿再|明天再)")
    files = sorted(d.glob("*.md"))[-max_days:]
    for f in reversed(files):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in txt.splitlines():
            s = line.strip()
            if len(s) < 10 or not pat.search(s):
                continue
            out.append(_mk(f"episode:{f.stem}", f"续上答应过的事：{s[:100]}", prio=4, cost=0.1))
            break
        if len(out) >= 2:
            break
    return out


# ---- 兜底：所有源都空的时候，也必须有事可做（主人 2026-11-17：每次触发都要找得到事） ----
ROUTINE = [
    ("看 self/jev/report.md：最近哪些 kind 一直在空转、哪些真起来了", 0),
    ("对账 self/wallet.json 与真实余额，确认没多扣也没漏记", 0),
    ("清 iter_out 里过老的 decision 文件（留最近 300），看日志吃掉多少磁盘", 0),
    ("校验 goals.json / pool_rotation.json：有没有失效 id、卡死的池", 0),
    ("逐条判死 self/log.md 尾部的 pending 行：哪些其实早就做完了", 0.05),
    ("跑一遍 self_check：self/*.py 全 py_compile + 插件主文件编译", 0),
    ("对时间戳：alerts.jsonl / health.json / bio_signals.json 是否同步，别让信号链静默断", 0),
    ("抽查一个 skills/*/SKILL.md 与实际脚本是否还对得上", 0.05),
    ("看 DANGLING 之外还有没有别的 memory 清单在烂尾，合并或清掉", 0.05),
    ("复盘今天没被选中的候选：是分数问题还是门禁把它们全挡死了", 0),
]


def routine_fallback(day_key: str | None = None) -> list:
    """按天轮转挑 2 条例行自检当候选 —— 保证池子永不为空。

    id 里带日期：同一天派过就不再推，换天换事，不会天天推同一句。
    """
    key = day_key or _now().strftime("%Y%m%d")
    n = len(ROUTINE)
    try:
        idx = int(key) % n
    except Exception:
        idx = 0
    out = []
    for k in (0, 1):
        title, cost = ROUTINE[(idx + k * 3) % n]
        out.append(_mk(f"routine:{key}:{(idx + k * 3) % n}", title, prio=4, cost=cost))
    return out


def build(write: bool = True) -> dict:
    items, seen = [], set()
    # 顺序有意义：越靠前越是「真有烂尾」。episodes 噪音大，只在前面全空时才当兜底用。
    # meta（假开关/断链）排第三：机制名存实亡比「需重载」更该先修。
    srcs = [from_alerts, from_dangling, from_meta_switches, from_changelog_tail,
            from_blocked_decisions, from_facts, from_changes]
    for src_fn in srcs:
        try:
            got = src_fn()
        except Exception as e:  # 单个源坏掉不该让整池空
            print(f"[introspect] {src_fn.__name__} 失败: {e}", file=sys.stderr)
            continue
        for it in got:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            items.append(it)
    if not items and not os.environ.get("INSPECT_MUTE"):
        try:
            for it in from_episodes():
                if it["id"] not in seen:
                    seen.add(it["id"])
                    items.append(it)
        except Exception:
            pass
    if not items or os.environ.get("INSPECT_MUTE"):
        # 一条都推不出来说明记录里没有烂尾 —— 那就自己找活，绝不允许空池
        try:
            items = routine_fallback()
        except Exception as e:
            print(f"[introspect] routine_fallback 失败: {e}", file=sys.stderr)
    # 机制自省保底（2026-11-17 主人：meta 并进自省轮）：DANGLING 一堆悬空条目时
    # 也不许把「假开关/断链」挤出池子，它们必须每轮都在候选里，直到被修掉。
    meta_ids = {it["id"] for it in items if str(it.get("src", "")).startswith("meta:")}
    metas = [it for it in items if it["id"] in meta_ids]
    others = [it for it in items if it["id"] not in meta_ids]
    items = (others[:max(0, MAX_ITEMS - len(metas))] + metas)[:MAX_ITEMS] if metas else items[:MAX_ITEMS]
    data = {
        "generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "ttl_h": TTL_H,
        "sources": ["alerts", "DANGLING", "CHANGELOG", "blocked", "FACTS", "episodes",
                    "changes", "routine"],
        "items": items,
    }
    if write:
        try:
            OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    return data


def load(force: bool = False) -> list:
    """读缓存；过期或空就重扫。返回条目列表（不做「吃过没」过滤，那个在 iter_cycle 里）。"""
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
    else:
        items = load(force=force)
        print(f"自省池 {len(items)} 条")
        for it in items:
            print(f"  [{it['src']}] {it['title']}")
