# -*- coding: utf-8 -*-
"""housekeep.py — 自迭代循环的「总结 + 瘦身」环节。

为什么要有：iter_cycle.py 每轮写一个 decision_*.json、往 log.md 追加一行，
mimic 每轮出图几十 MB，备份文件越攒越多。不管的话存储是线性膨胀。

两个动作，都遵守「可逆优先」：
  1. summarize()  把 iter_out/*.json + log.md 聚合成本周/历史统计，写进
                  self/iter_summary.md 和 self/iter_stats.json（累计，不丢历史总量）
  2. slim()       把冷数据移进 self/.cold/ 并 zip，**不删除**；
                  唯一真删的是 __pycache__（纯可再生）。

用法（不要用 PATH 上的 python，那是 Store 空壳）：
  真解释器 = <uv>/python/cpython-3.12.x-windows-x86_64-none/python.exe
  python housekeep.py             # 预演，只打印
  python housekeep.py --apply     # 真执行
  python housekeep.py --report    # 只补一次总结，不动文件

保留窗口（可用 --keep-days / --keep-baks 覆盖）：
  iter_out/decision_*.json  14 天内的留在原地
  log.md                    超过 300KB 就把旧段切到 log_archive_YYYYMM.md（内容是搬运，不丢）
  *.bak* 备份               每个前缀保留最近 3 个；更早的先 diff 成版本日志写进
                            rollback_notes.md（改了什么/干什么用/防什么问题/怎么退），
                            再压进 .cold —— 想回退就一版版读日志，不靠留一堆整文件
  tmp_*.py                  7 天没动过就进 .cold
  practice/mimic/gen        最近 7 天 + runs.jsonl 里分数 top5 的留下，其余进 .cold
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
SELF = Path(__file__).resolve().parent
WORK = SELF.parent
ITER_OUT = SELF / "iter_out"
LOG = SELF / "log.md"
SUMMARY = SELF / "iter_summary.md"
STATS = SELF / "iter_stats.json"
ROLLBACK_NOTES = SELF / "rollback_notes.md"
COLD = SELF / ".cold"
RUNS = SELF / "practice" / "mimic" / "runs.jsonl"
GEN = SELF / "practice" / "mimic" / "gen"

LOG_ROTATE_BYTES = 300 * 1024
MOVE_EXTRA_GEN_KEEP = 5


def now() -> datetime:
    return datetime.now(CST)


def mb(n: int) -> float:
    return round(n / 1024 / 1024, 1)


def dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


# ---------------- 1. 总结 ----------------

def _read_decisions() -> list[dict]:
    rows = []
    for f in sorted(ITER_OUT.glob("decision_*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict):
                d["_file"] = f.name
                rows.append(d)
        except Exception:
            continue
    rows.sort(key=lambda d: (d.get("_file") or ""))
    return rows


def summarize(apply: bool) -> dict:
    """按天聚合 decision，产出累计统计。历史统计只增不减，裁剪 iter_out 也不会丢总量。"""
    rows = _read_decisions()
    prev = {}
    if STATS.exists():
        try:
            prev = json.loads(STATS.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    days: dict[str, dict] = defaultdict(lambda: {
        "rounds": 0, "picked": 0, "blocked": 0, "cost_rmb": 0.0,
        "pain_sum": 0.0, "pain_n": 0, "kinds": Counter(), "blocked_reasons": Counter(),
        "titles": Counter(),
    })
    for d in rows:
        ts = str(d.get("ts") or "")
        day = ts[:10] or "unknown"
        b = days[day]
        b["rounds"] += 1
        if d.get("pick"):
            b["picked"] += 1
            b["kinds"][(d["pick"] or {}).get("kind") or "?"] += 1
            t = str((d["pick"] or {}).get("title") or "")[:28]
            if t:
                b["titles"][t] += 1
        if d.get("blocked"):
            b["blocked"] += 1
            b["blocked_reasons"][str(d["blocked"])] += 1
        try:
            b["cost_rmb"] += float(d.get("cost_rmb") or 0)
        except Exception:
            pass
        try:
            b["pain_sum"] += float(d.get("pain") or 0)
            b["pain_n"] += 1
        except Exception:
            pass

    # 累计：老日子保留，新日子覆盖（同一天重算不重复计）
    cum_days = dict(prev.get("days") or {})
    for day, b in days.items():
        cum_days[day] = {
            "rounds": b["rounds"], "picked": b["picked"], "blocked": b["blocked"],
            "cost_rmb": round(b["cost_rmb"], 3),
            "pain_avg": round(b["pain_sum"] / b["pain_n"], 1) if b["pain_n"] else None,
            "kinds": dict(b["kinds"]), "blocked_reasons": dict(b["blocked_reasons"]),
            "titles": dict(b["titles"]),
            "archived_at": cum_days.get(day, {}).get("archived_at") or now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

    ordered = sorted(cum_days.items())
    tot_rounds = sum(v["rounds"] for _, v in ordered)
    tot_cost = round(sum(v["cost_rmb"] for _, v in ordered), 3)
    tot_picked = sum(v["picked"] for _, v in ordered)
    kinds_all = Counter()
    for _, v in ordered:
        kinds_all.update(v.get("kinds") or {})
    last7 = [v for d, v in ordered if d >= (now() - timedelta(days=7)).strftime("%Y-%m-%d")]
    stats = {
        "updated": now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_rounds": tot_rounds,
        "total_picked": tot_picked,
        "total_cost_rmb": tot_cost,
        "pick_rate": round(tot_picked / tot_rounds, 3) if tot_rounds else None,
        "kinds_all": dict(kinds_all),
        "last7": {
            "rounds": sum(v["rounds"] for v in last7),
            "picked": sum(v["picked"] for v in last7),
            "cost_rmb": round(sum(v["cost_rmb"] for v in last7), 3),
            "pain_avg": (round(sum(v["pain_avg"] for v in last7 if v["pain_avg"] is not None)
                               / max(1, len([v for v in last7 if v["pain_avg"] is not None])), 1)
                         if last7 else None),
        },
        "days": dict(ordered),
    }

    # markdown：只留最近 14 天明细 + 累计，避免自己又变成大文件
    lines = ["# 自迭代轮次总结", "",
             f"自动生成：{stats['updated']}  累计 {tot_rounds} 轮，选中 {tot_picked} 轮"
             f"（{stats['pick_rate']}），花费 {tot_cost} rmb", "",
             "## 近 7 天", ""]
    if last7:
        lines.append(f"- 轮数 {stats['last7']['rounds']}｜选中 {stats['last7']['picked']}"
                     f"｜花费 {stats['last7']['cost_rmb']} rmb｜平均 pain {stats['last7']['pain_avg']}")
    else:
        lines.append("- 无")
    lines += ["", "## 按天（近 14 天）", "",
              "| 日期 | 轮数 | 选中 | 花费 | pain均值 | 阻塞 | 主要类型 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for day, v in ordered[-14:]:
        kk = ",".join(f"{k}×{n}" for k, n in sorted((v.get("kinds") or {}).items(), key=lambda x: -x[1])[:3]) or "-"
        bb = ",".join(f"{k}×{n}" for k, n in (v.get("blocked_reasons") or {}).items()) or "-"
        lines.append(f"| {day} | {v['rounds']} | {v['picked']} | {v['cost_rmb']} | "
                     f"{v['pain_avg'] if v['pain_avg'] is not None else '-'} | {bb} | {kk} |")
    lines += ["", "## 近 14 天在做什么（每轮目标，去重后最多 3 条）", ""]
    for day, v in ordered[-14:]:
        tt = [f"{n}×{c}" if c > 1 else n for n, c in
              sorted((v.get("titles") or {}).items(), key=lambda x: -x[1])[:3]]
        lines.append(f"- {day}：" + ("；".join(tt) if tt else "（无选中）"))
    lines += ["", "## 累计类型分布", "",
              *(f"- {k}: {n}" for k, n in sorted(kinds_all.items(), key=lambda x: -x[1])), ""]

    if apply:
        STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    return {"rows": len(rows), "total_rounds": tot_rounds, "written": apply}



# ---------------- 1.5 备份 → 版本日志 ----------------
BAK_RE = re.compile(r"^(?P<base>.+?)\.bak(?P<tag>.*)$")


def _is_code(p: Path) -> bool:
    return p.suffix.lower() in (".py", ".ps1", ".cmd", ".bat", ".json", ".md", ".txt")


def _purpose(cur: Path, lines: list[str]) -> str:
    """按文件类型各自找「这文件干什么用的」，找不到就直说，绝不猜。"""
    suf = cur.suffix.lower()
    if suf == ".json":
        try:
            d = json.loads("\n".join(lines))
            for k in ("_note", "note", "desc", "description", "_about"):
                if isinstance(d.get(k), str):
                    return d[k][:100]
            return "JSON 数据文件，顶层键：" + ", ".join(list(d)[:8])
        except Exception:
            return "JSON（解析失败，看下面的 diff）"
    if suf == ".py":
        body = "\n".join(lines)
        m = re.search(r'"""(.*?)"""', body, re.S) or re.search(r"'''(.*?)'''", body, re.S)
        if m:
            for l in m.group(1).splitlines():
                l = l.strip()
                if l and not l.startswith(("#", "-", "=")):
                    return l[:100]
        for l in lines[:12]:
            t = l.strip().lstrip("#").strip()
            if t and not t.startswith(("import", "from", "#!/", "-*-")):
                return t[:100]
        return "Python 脚本（没写说明）"
    if suf == ".md":
        for l in lines[:6]:
            if l.startswith("#"):
                return l.lstrip("#").strip()[:100]
        return "Markdown 文档"
    return "（非代码/配置，未识别类型）"


def bak_note(bak: Path, old_text: str | None = None, mtime: float | None = None,
             src: str = "") -> str:
    """把一个 .bak 备份转成一段人能读的版本记录：改了什么 / 干什么用 / 防什么问题 / 怎么退。"""
    m = BAK_RE.match(bak.name)
    cur = SELF / (m.group("base") if m else bak.name.split(".bak")[0])
    ts = mtime if mtime is not None else bak.stat().st_mtime
    size = len(old_text.encode()) if old_text is not None else bak.stat().st_size
    out = [f"\n### {bak.name}",
           f"- 备份时间：{datetime.fromtimestamp(ts, CST):%Y-%m-%d %H:%M}"
           f"　体积：{mb(size)}MB　标签：{(m.group('tag').strip('._') or '-') if m else '-'}"
           + (f"　来源：{src}" if src else "")]
    if not cur.exists():
        out.append(f"- 对应文件 `{cur.name}` 现在已不存在：本条只留档，原文件在 `.cold/*.zip` 里")
        return "\n".join(out)
    try:
        old = (old_text if old_text is not None
               else bak.read_text(encoding="utf-8", errors="replace")).splitlines()
        new = cur.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        out.append(f"- 内容读不出来（{type(e).__name__}），原文件在 `.cold/*.zip` 里")
        return "\n".join(out)

    # 用途：从文件自己身上摘一句说明，免得以后要凭记忆才知道这是什么
    purpose = _purpose(cur, new)
    goals_hit = []
    try:
        gj = (SELF / "goals.json").read_text(encoding="utf-8-sig")
        if cur.name in gj:
            goals_hit = [g.get("id") for g in json.loads(gj).get("goals", [])
                         if cur.name in json.dumps(g, ensure_ascii=False)][:4]
    except Exception:
        pass

    diff = list(difflib.unified_diff(old, new, fromfile=bak.name, tofile=cur.name, lineterm="", n=2))
    add = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    def defs(sign):
        return [re.sub(r"^(def|class)\s+(\w+).*", r"\2", l[1:].strip())
                for l in diff if l.startswith(sign) and re.match(r"^\s*(def |class )", l[1:])]
    out.append(f"- 这个文件是干什么的：{purpose or '（没读到说明，自己看文件头）'}")
    if goals_hit:
        out.append(f"- 它服务的自迭代目标：{', '.join(goals_hit)}")
    out.append(f"- 与当前版本差异：+{add} / -{rem} 行")
    da, dd = defs("+"), defs("-")
    both = set(da) & set(dd)
    if da:
        txt = [f"{n}（改签名）" if n in both else n for n in da[:12]]
        out.append(f"- 之后新增的函数/类：{', '.join(txt)}{' …' if len(da) > 12 else ''}")
    pure_del = [n for n in dd if n not in both]
    if pure_del:
        out.append(f"- 之后被删掉的函数/类：{', '.join(pure_del[:12])}{' …' if len(pure_del) > 12 else ''}")
    # 改动摘要：只留增删行，最多 40 行，够看懂意图
    body = [l for l in diff if l[:1] in ("+", "-") and not l.startswith(("+++", "---"))]
    if body:
        out.append(f"- 改动摘要（共 {len(body)} 行增删，此处留前 40 行）：")
        out.append("```diff")
        out.extend(body[:40])
        if len(body) > 40:
            out.append(f"...（余 {len(body) - 40} 行见 .cold/*.zip 里的原文件）")
        out.append("```")
    out.append("- 干什么用：这是改动前的快照，用来确认那次改动的出发点")
    out.append("- 防什么问题：改动把原来能跑的逻辑改坏（回归）；改完才发现方向错了")
    out.append(f"- 出问题怎么退：照本节 diff 反打回 `{cur.name}`，或从 "
               f"`.cold/{datetime.fromtimestamp(ts, CST):%Y%m}.zip` 取原文件覆盖，"
               f"改完跑一次 `iter_cycle.py`（不带 --commit）确认能起")
    return "\n".join(out)



def backfill_notes(apply: bool) -> int:
    """把已经压进 .cold 的老备份补成版本日志（只补没记过的）。"""
    if not COLD.exists():
        return 0
    have = set()
    if ROLLBACK_NOTES.exists():
        have = {l[4:].strip() for l in ROLLBACK_NOTES.read_text(encoding="utf-8", errors="replace").splitlines()
                if l.startswith("### ")}
    todo: list[str] = []
    for zp in sorted(COLD.glob("*.zip")):
        try:
            with zipfile.ZipFile(zp) as z:
                for info in z.infolist():
                    name = Path(info.filename).name
                    if ".bak" not in name or name in have:
                        continue
                    try:
                        text = z.read(info).decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    todo.append(bak_note(SELF / name, old_text=text,
                                         mtime=info.date_time and datetime(*info.date_time, tzinfo=CST).timestamp(),
                                         src=f"{zp.name}"))
                    have.add(name)
        except Exception:
            continue
    if apply and todo:
        with open(ROLLBACK_NOTES, "a", encoding="utf-8") as fh:
            fh.write("\n## 历史补记（从 .cold 里的整文件备份回溯）\n" + "\n".join(todo) + "\n")
    return len(todo)


# ---------------- 2. 瘦身 ----------------

def _cold(path: Path, reason: str, apply: bool, log: list) -> int:
    """把文件/目录搬进 .cold 并按月 zip，原文件删除（内容可回取）。返回搬走的字节数。"""
    size = dir_size(path)
    log.append(f"  [冷存] {path.relative_to(SELF)}  {mb(size)}MB  ({reason})")
    if not apply:
        return size
    COLD.mkdir(exist_ok=True)
    month = now().strftime("%Y%m")
    stamp = now().strftime("%Y%m%d_%H%M%S")
    zpath = COLD / f"{month}.zip"
    try:
        with zipfile.ZipFile(zpath, "a", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            if path.is_file():
                z.write(path, arcname=f"{path.name}")
            else:
                for f in path.rglob("*"):
                    if f.is_file():
                        z.write(f, arcname=f"{path.relative_to(SELF)}")
    except Exception as e:
        log.append(f"    ! zip 失败，跳过不删：{e}")
        return 0
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except Exception as e:
        log.append(f"    ! 删除失败（已进 zip）：{e}")
        return size
    # 记录一份索取说明，方便以后回取
    with open(COLD / "INDEX.txt", "a", encoding="utf-8") as f:
        f.write(f"{stamp}\t{month}.zip\t{path.relative_to(SELF)}\t{reason}\t{mb(size)}MB\n")
    return size


def slim(apply: bool, keep_days: int = 14, keep_baks: int = 3, gen_days: int = 7) -> dict:
    log: list[str] = []
    freed = 0
    cutoff = now() - timedelta(days=keep_days)
    bak_cut = now() - timedelta(days=7)   # 保留最近 keep_baks 个，再老一周就冷存

    # a) __pycache__ 直接删（可再生，不算数据）
    for pc in list(SELF.rglob("__pycache__")) + list((WORK / "self").rglob("__pycache__")):
        if not pc.is_dir():
            continue
        sz = dir_size(pc)
        log.append(f"  [删除] {pc.relative_to(SELF)}  {mb(sz)}MB  (字节码缓存，可再生)")
        freed += sz
        if apply:
            shutil.rmtree(pc, ignore_errors=True)

    # b) 备份文件：每个前缀保留最近 keep_baks 个，其余进 .cold
    groups: dict[str, list[Path]] = defaultdict(list)
    for f in SELF.iterdir():
        if not f.is_file():
            continue
        m = re.match(r"^(.+?)\.bak", f.name)
        if m:
            groups[m.group(1)].append(f)
    notes: list[str] = []
    for prefix, files in sorted(groups.items()):
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[keep_baks:]:
            age_d = (now() - datetime.fromtimestamp(f.stat().st_mtime, CST)).days
            if f.stat().st_mtime < bak_cut.timestamp():
                # 先转成人能读的版本日志，再收走原文件 —— 以后靠日志一版版往前读
                if _is_code(f):
                    notes.append(bak_note(f))
                freed += _cold(f, f"转版本日志后冷存（{age_d} 天）", apply, log)
    if apply and notes:
        with open(ROLLBACK_NOTES, "a", encoding="utf-8") as fh:
            fh.write("\n".join(notes) + "\n")
        log.append(f"  [日志] {len(notes)} 个旧备份已写成版本记录 → rollback_notes.md")

    # c) tmp_*.py 临时脚本：7 天没动就冷存
    for f in SELF.glob("tmp_*.py"):
        if f.stat().st_mtime < (now() - timedelta(days=7)).timestamp():
            freed += _cold(f, "临时脚本超 7 天", apply, log)

    # d) iter_out：超过 keep_days 的 decision 冷存（总结已经吃进去了）
    old = []
    for f in ITER_OUT.glob("decision_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8-sig"))
            ts = str(d.get("ts") or "")
            dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            if dt < cutoff:
                old.append(f)
        except Exception:
            old.append(f)  # 读不出来的一律当老的
    if old:
        batch = len(old)
        # 打包成一个目录形式冷存，避免几百次 zip 写
        if apply:
            COLD.mkdir(exist_ok=True)
            zpath = COLD / f"{now().strftime('%Y%m')}_iterout.zip"
            with zipfile.ZipFile(zpath, "a", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for f in old:
                    z.write(f, arcname=f"iter_out/{f.name}")
                    f.unlink(missing_ok=True)
            with open(COLD / "INDEX.txt", "a", encoding="utf-8") as fi:
                fi.write(f"{now().strftime('%Y%m%d_%H%M%S')}\t{zpath.name}\titer_out/*({batch}个)\t"
                         f"超 {keep_days} 天，已入总结\t\n")
        sz = sum(f.stat().st_size for f in old)
        freed += sz
        log.append(f"  [冷存] iter_out 里 {batch} 个旧 decision  {mb(sz)}MB  (超 {keep_days} 天)")

    # e) log.md 太大就切旧段（搬运，不丢内容）
    if LOG.exists() and LOG.stat().st_size > LOG_ROTATE_BYTES:
        text = LOG.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        keep_n = max(200, len(lines) // 3)
        old_part, keep_part = lines[:-keep_n], lines[-keep_n:]
        arc = SELF / f"log_archive_{now().strftime('%Y%m')}.md"
        log.append(f"  [轮转] log.md {mb(LOG.stat().st_size)}MB → 旧 {len(old_part)} 行搬到 {arc.name}")
        freed += 0
        if apply:
            with open(arc, "a", encoding="utf-8") as f:
                f.write("".join(old_part))
            LOG.write_text("".join(keep_part), encoding="utf-8")

    # e2) rollback_notes.md 自己也会涨：超 300KB 按「### 条目」切段轮转
    if ROLLBACK_NOTES.exists() and ROLLBACK_NOTES.stat().st_size > LOG_ROTATE_BYTES:
        text = ROLLBACK_NOTES.read_text(encoding="utf-8", errors="replace")
        chunks = re.split(r"(?m)^(?=### )", text)
        head, entries = chunks[0], chunks[1:]
        keep_n = max(6, len(entries) // 3)
        arc = SELF / f"rollback_notes_{now():%Y%m}.md"
        log.append(f"  [轮转] rollback_notes.md {mb(len(text.encode()))}MB → 旧 {len(entries) - keep_n} 条搬到 {arc.name}")
        if apply:
            with open(arc, "a", encoding="utf-8") as f:
                f.write(head + "".join(entries[:-keep_n]))
            ROLLBACK_NOTES.write_text(head + "".join(entries[-keep_n:]), encoding="utf-8")

    # f) mimic 出图：保留最近 7 天 + runs.jsonl 里 top5
    if GEN.exists():
        top_names: set[str] = set()
        try:
            rows = [json.loads(l) for l in RUNS.read_text(encoding="utf-8").splitlines() if l.strip()]
            scored = [r for r in rows if isinstance(r.get("score"), (int, float))]
            scored.sort(key=lambda r: -r["score"])
            for r in scored[:MOVE_EXTRA_GEN_KEEP]:
                p = str(r.get("image") or r.get("file") or "")
                if p:
                    top_names.add(Path(p).name)
        except Exception:
            pass
        gen_files = [f for f in GEN.glob("*.png") if f.is_file()]
        victim = [f for f in gen_files
                  if f.stat().st_mtime < (now() - timedelta(days=gen_days)).timestamp()
                  and f.name not in top_names]
        if victim:
            sz = sum(f.stat().st_size for f in victim)
            log.append(f"  [冷存] mimic/gen 里 {len(victim)} 张旧图  {mb(sz)}MB  "
                       f"(超 {gen_days} 天且不在 top{MOVE_EXTRA_GEN_KEEP})")
            freed += sz
            if apply:
                COLD.mkdir(exist_ok=True)
                zpath = COLD / f"{now().strftime('%Y%m')}_mimicgen.zip"
                with zipfile.ZipFile(zpath, "a", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                    for f in victim:
                        z.write(f, arcname=f"mimic/gen/{f.name}")
                        f.unlink(missing_ok=True)
                with open(COLD / "INDEX.txt", "a", encoding="utf-8") as fi:
                    fi.write(f"{now().strftime('%Y%m%d_%H%M%S')}\t{zpath.name}\t"
                             f"mimic/gen/*({len(victim)}张)\t超 {gen_days} 天非 top5\t{mb(sz)}MB\n")

    # 大件只报告（模型删了要重下，不自动动）
    models = SELF / "practice" / "mimic" / "models"
    msz = dir_size(models)
    if msz > 100 * 1024 * 1024:
        log.append(f"  [保留] mimic/models  {mb(msz)}MB  (模型权重，删了要重新下载，只报不动)")

    return {"freed_mb": mb(freed), "log": log, "apply": apply}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", action="store_true", help="只补总结")
    ap.add_argument("--keep-days", type=int, default=14)
    ap.add_argument("--keep-baks", type=int, default=3)
    ap.add_argument("--gen-days", type=int, default=7, help="mimic 出图保留天数")
    ap.add_argument("--backfill", action="store_true", help="把 .cold 里的老备份补成版本日志")
    args = ap.parse_args()

    before = dir_size(SELF)
    mode = "执行" if args.apply else "预演"
    print(f"[{now():%Y-%m-%d %H:%M:%S}] housekeep {mode}｜self/ 当前 {mb(before)}MB")
    s = summarize(args.apply)
    print(f"总结：扫到 {s['rows']} 条 decision，累计 {s['total_rounds']} 轮"
          f"{'（已写入 iter_summary.md）' if args.apply else '（预演，未写）'}")
    if args.report:
        return 0
    if args.backfill:
        n = backfill_notes(args.apply)
        print(f"补记：{n} 个老备份{'已写入' if args.apply else '待写入'} rollback_notes.md")
        return 0
    r = slim(args.apply, args.keep_days, args.keep_baks, args.gen_days)
    for line in r["log"]:
        print(line)
    if not r["log"]:
        print("  无需瘦身")
    after = dir_size(SELF) if args.apply else before
    print(f"结果：{mode} 可回收/已回收 {r['freed_mb']}MB，self/ {mb(before)}MB → {mb(after)}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
