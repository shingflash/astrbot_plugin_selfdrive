# -*- coding: utf-8 -*-
"""灵感发生器：拿两条不同来源的旧料撞一下，产一条待验证假设。

规则见 self/linkage.md 第六节——
  · 料要杂，不同来源相撞
  · 产出只能是「如果 A 像 B，那么 C」，C 必须能测
  · 当场配五分钟可逆实验，结果写进 self/inspiration.md
  · 不成立也要留档；只进待验证清单，不许直接改代码

脚本只负责配对、落池、写台账，C 由执行轮来写。

用法:
  python inspiration.py --dry     只看会撞出什么，不落池
  python inspiration.py           撞 1 条进池（池里已有 2 条未验证灵感则不动）
  python inspiration.py --show    看台账和池里的灵感条
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SELF = Path(__file__).resolve().parent
WS = SELF.parent
DATA = WS.parents[1]                 # <data>
POOL = SELF / "goals_curiosity.json"
LEDGER = SELF / "inspiration.md"
CST = timezone(timedelta(hours=8))
MAX_LIVE = 2          # 池里同时挂 2 条就够，别挤掉好奇
TTL_H = 168.0         # 灵感不追热点，一周
SKIP = re.compile(r"^(```|>|#|\||\s*$)")

VERIFY_TPL = (
    "按 self/linkage.md 第六节做一次灵感验证。"
    "素材A（{an}）：{a}；素材B（{bn}）：{b}。"
    "先写一句「如果 A 像 B，那么 C」，C 必须是能测的机制猜想；"
    "再配一个五分钟内做完、可撤回的实验，跑完在 self/inspiration.md 记一条："
    "假设 / 实验做法 / 结果（成立·不成立·测不了）/ 下一步。"
    "测不了也要写清为什么，不许空手结项。"
)


def now_s() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


# 料要带"机制味"，闲聊片段撞出来的是废话
KEYS = ("因为", "所以", "机制", "原理", "坑", "失败", "修", "函数", "端口", "依赖",
        "验证", "假设", "错", "代价", "边界", "触发", "进程", "配置", "规则")


def _clean(s: str) -> str:
    s = re.sub(r"^[-*]\s*", "", s.strip())
    s = re.sub(r"^\d{1,2}:\d{2}\s*(\[[^\]]*\])?\s*", "", s)
    s = s.replace("**", "").replace("`", "")
    s = re.split(r"\s*\(来源:", s)[0]
    return s.strip()


BAD = ("|", "File written", "Command completed", "exit code", "Select-String",
       "python.exe", "Get-Content", "Copy-Item", "Get-ChildItem")


def _ok(s: str) -> bool:
    """表格行、工具输出、英文日志、模板占位都不算料——撞出来只有噪音。"""
    if any(b in s for b in BAD):
        return False
    if s[:1] in "（(【":
        return False
    zh = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return zh / max(1, len(s)) >= 0.35


def _lines(p: Path, limit: int = 300) -> list[str]:
    """抽带机制味的长句，按关键词命中数排序，只留前 40%——宁缺勿滥。"""
    try:
        raw = p.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    except Exception:
        return []
    scored: list[tuple[int, str]] = []
    for ln in raw[-limit:]:
        s = ln.strip()
        if SKIP.match(s):
            continue
        s = _clean(s)
        if not (14 <= len(s) <= 100) or not _ok(s):
            continue
        if "待写" in s or "实验：" in s:
            continue
        scored.append((sum(1 for k in KEYS if k in s), s))
    scored.sort(key=lambda x: -x[0])
    keep = max(5, len(scored) * 2 // 5)
    return [s for _, s in scored[:keep]]


def _episode_lines(n_files: int = 4) -> list[str]:
    d = WS / "memory" / "episodes"
    if not d.is_dir():
        return []
    out: list[str] = []
    for f in sorted(d.glob("*.md"))[-n_files:]:
        out += _lines(f)
    return out


def sources() -> dict[str, list[str]]:
    """不同来源各备一堆料。同一来源内部撞不算灵感，那是回音。"""
    s: dict[str, list[str]] = {
        "历史原话": _episode_lines(),
        "改动记录": _lines(SELF / "changes.md"),
        "当前事实": _lines(WS / "memory" / "FACTS.md"),
        "自迭代流水": _lines(SELF / "log.md"),
        "技能库": [p.name for p in (DATA / "skills").glob("*") if p.is_dir()]
        if (DATA / "skills").is_dir() else [],
        "自建技能": [p.name for p in (WS / "skills").glob("*") if p.is_dir()]
        if (WS / "skills").is_dir() else [],
    }
    return {k: v for k, v in s.items() if v}


def load_pool() -> dict:
    try:
        d = json.loads(POOL.read_text(encoding="utf-8-sig"))
        if isinstance(d, dict):
            d.setdefault("items", [])
            return d
    except Exception:
        pass
    return {"generated_at": now_s(), "items": []}


def live_inspiration(doc: dict) -> list[dict]:
    return [it for it in (doc.get("items") or [])
            if isinstance(it, dict) and it.get("kind") == "inspiration"]


def make(dry: bool = False) -> int:
    srcs = sources()
    names = [n for n, v in srcs.items() if v]
    if len(names) < 2:
        print("[skip] 可撞的料源不足 2 个")
        return 1

    doc = load_pool()
    live = live_inspiration(doc)
    if len(live) >= MAX_LIVE and not dry:
        print(f"[skip] 池里还有 {len(live)} 条未验证灵感，先消化再加")
        return 0

    a_name, b_name = random.sample(names, 2)
    a = random.choice(srcs[a_name])
    b = random.choice(srcs[b_name])
    title = f"灵感：{a[:22]} × {b[:22]}"
    iid = "insp_" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    item = {
        "id": iid,
        "kind": "inspiration",
        "title": title,
        "keyword": f"{a[:24]} / {b[:24]}",
        "curiosity_cat": "灵感",
        "priority": 5,
        "cost_rmb": 0,
        "source": f"撞料：{a_name}×{b_name}",
        "source_url": "",
        "ttl_h": TTL_H,
        "added_at": now_s(),
        "tier": "t2_write_low",
        "verify": VERIFY_TPL.format(an=a_name, a=a, bn=b_name, b=b),
    }

    if dry:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0

    # 同一个 A/B 组合撞过了就不再进池（台账里留着，翻得到）
    for it in live:
        if it.get("id") == iid:
            print(f"[skip] 这条撞过了：{iid}")
            return 0

    doc["items"].append(item)
    doc["items"] = doc["items"][-20:]      # 池子别无限长
    POOL.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    # 台账：入池就落一行，池子被好奇刷新冲掉也不丢线索
    if not LEDGER.exists():
        LEDGER.write_text(
            "# 灵感台账（待验证 → 已定性）\n\n"
            "> 规格见 `self/linkage.md` 第六节。每条必须有可做可撤回的实验和结论。\n\n",
            encoding="utf-8")
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(f"- [{now_s()}] 入池 `{iid}` **{title}**\n"
                f"  - A（{a_name}）：{a}\n"
                f"  - B（{b_name}）：{b}\n"
                f"  - 待写：如果 A 像 B，那么 C？ / 实验： / 结果：\n")
    print(f"[ok] {iid} {title}")
    return 0


def show() -> int:
    doc = load_pool()
    live = live_inspiration(doc)
    print(f"池内灵感 {len(live)} 条（上限 {MAX_LIVE}）：")
    for it in live:
        print(f"  - {it.get('id')} {it.get('title')}")
    if LEDGER.exists():
        rows = LEDGER.read_text(encoding="utf-8").splitlines()
        print(f"\n台账 {len(rows)} 行，末 12 行：")
        for ln in rows[-12:]:
            print("  " + ln)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--show" in args:
        return show()
    return make(dry="--dry" in args)


if __name__ == "__main__":
    raise SystemExit(main())
