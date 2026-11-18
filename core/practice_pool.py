# -*- coding: utf-8 -*-
"""练习池（2026-11-10 起，2026-11-17 主人重构为第 7 池）—— 补理论和实操的差距。

主人原话（2026-11-17 18:50）：
  · 这条路是实践和实验：空有灵感、空有数据，一用就出问题，练习就是拿来填这个坑。
  · 学过的东西必须真落地一次：学了绘画却一幅没画过，等于没学；音乐同理。
  · 差软件找我申请装；本地已有（比如 Unity）就在不碰已有工程/设置的前提下开新工程练手。
  · 练手时解决的错误越多分越高，未解决的错误越多分越低。

设计：
  · 冷库 self/practice/practice_lib.json 存全部练习任务（why / verify / sandbox / needs_software）。
  · 独立池文件 self/goals_practice.json，kind="practice"，由 iter_cycle 的 practice 池读。
  · 缺软件不自己装：生成一条「申请安装 X」的只读条目，等主人批。
  · 评分 self/practice/score.json：score = 累计解决 − 累计未解决，另记比率。
    分数只做记录与展示，**不**接回选事排序（自评分排序那次已被证明反了）。

用法：
  python self/practice_pool.py                 # 冷库全排（缺软件/有软件都入池）
  python self/practice_pool.py --n 5
  python self/practice_pool.py --show
  python self/practice_pool.py --reset
  python self/practice_pool.py --add 标题 --need blender
  python self/practice_pool.py --score t2i_baseline 3 1   # 解决 3 个、未解决 1 个
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:                                    # Windows 控制台默认 GBK，中文直接 print 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
POOL = SELF / "goals_practice.json"          # 独立第 7 池（2026-11-17）
LIB = SELF / "practice" / "practice_lib.json"
LOG = SELF / "practice" / "log.md"
SCORE = SELF / "practice" / "score.json"
ITER_OUT = SELF / "iter_out"
STAMP = SELF / "practice" / "practice_pool_state.json"

KIND = "practice"
ID_PREFIX = "prac_"
REQ_PREFIX = "req_"
PRACTICE_WINDOW = 3
TTL_H = 24 * 30.0                       # 练习任务不吃热点，挂一个月没做才提示
SCAN_FILES = 400
CST = timezone(timedelta(hours=8))


def now_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def load_lib() -> dict:
    if not LIB.exists():
        # 公开版：冷库是私货位（自己累积的练习记录），没有就按空表跑，不拦整轮
        return {"tasks": [], "count": 0, "updated_at": None}
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
    return {"generated_at": now_str(), "ttl_h": TTL_H, "sources": {}, "items": []}


def consumed_ids() -> set:
    ids = set()
    if not ITER_OUT.exists():
        return ids
    for f in sorted(ITER_OUT.glob("decision_*.json"))[-SCAN_FILES:]:
        try:
            j = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        for p in (j.get("pick"), (j.get("picks") or [{}])[0]):
            if isinstance(p, dict) and p.get("id"):
                ids.add(str(p["id"]))
    return ids


# ---------------------------------------------------------------- 软件检查
# 2026-11-17 19:29 修：旧实现拿「目录名匹配」当装了软件，
#   %LOCALAPPDATA%\Blender Foundation、%APPDATA%\obs-studio 这种**配置残留目录**
#   在软件卸载后还在，会被判成「已安装」→ 缺软件申请永远发不出去。
#   现在按 注册表卸载项 → 可执行文件 → which 三层判，必须真找到程序本体。
_ALIAS = {"blender": ["blender"], "unity": ["unity hub", "unity "],
          "pr": ["premiere pro"], "ae": ["after effects"],
          "ps": ["photoshop"], "davinci": ["davinci resolve"],
          "obs": ["obs studio"], "fl": ["fl studio"],
          "cubase": ["cubase"], "ableton": ["ableton live"],
          "nuke": ["nuke"], "剪映": ["capcut", "剪映"], "capcut": ["capcut"]}

_EXE_HINTS = {
    "premiere pro": [r"C:\Program Files\Adobe\Adobe Premiere Pro*\Adobe Premiere Pro.exe"],
    "after effects": [r"C:\Program Files\Adobe\Adobe After Effects*\Adobe After Effects.exe"],
    "photoshop": [r"C:\Program Files\Adobe\Adobe Photoshop*\Photoshop.exe"],
    "davinci resolve": [r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"],
    "obs studio": [r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"],
    "nuke": [r"C:\Program Files\Nuke*\nuke*.exe"],
}
_REG_CACHE: dict = {}


def _uninstall_entries() -> list:
    """注册表卸载项 (DisplayName, InstallLocation)。缓存，一天跑一次够用。"""
    if "rows" in _REG_CACHE:
        return _REG_CACHE["rows"]
    rows: list = []
    try:
        import winreg
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, path in roots:
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    sk = winreg.OpenKey(key, sub)
                except OSError:
                    continue
                try:
                    dn = str(winreg.QueryValueEx(sk, "DisplayName")[0] or "").strip()
                    loc = str(winreg.QueryValueEx(sk, "InstallLocation")[0] or "").strip()
                except OSError:
                    dn, loc = "", ""
                finally:
                    try:
                        winreg.CloseKey(sk)
                    except Exception:
                        pass
                if dn:
                    rows.append((dn, loc))
    except Exception:
        rows = []
    _REG_CACHE["rows"] = rows
    return rows


def software_ok(name: str) -> bool:
    """本机有没有这个软件：which → 注册表卸载项 → 程序本体 exe。

    查不到就当作缺（宁可不装也不乱猜）。注意配置目录（如 %APPDATA%\\obs-studio、
    %LOCALAPPDATA%\\Blender Foundation）**不算**安装证据——卸载后它们还在。
    """
    n = str(name or "").strip()
    if not n:
        return True
    if shutil.which(n) or shutil.which(n + ".exe"):
        return True
    keys = _ALIAS.get(n.lower(), [n.lower()])

    for dn, loc in _uninstall_entries():
        low = dn.lower()
        for k in keys:
            if k and k in low:
                if loc and not Path(loc).exists():
                    continue                       # 注册项残留、装的地方已经没了
                return True

    for k in keys:
        for pat in _EXE_HINTS.get(k, []):
            try:
                if glob.glob(pat):
                    return True
            except Exception:
                pass

    for k in keys:
        for base in (r"C:\Program Files", r"C:\Program Files (x86)",
                     os.path.expandvars(r"%LOCALAPPDATA%\Programs"),
                     os.path.expandvars(r"%LOCALAPPDATA%")):
            for pat in (f"{base}\\{k}*\\{k}*.exe", f"{base}\\{k}*\\**\\{k}*.exe"):
                try:
                    if glob.glob(pat, recursive=True):
                        return True
                except Exception:
                    pass
    return False


# ---------------------------------------------------------------- 条目
def item_of(t: dict, missing: list | None = None) -> dict:
    """冷库任务 → 池条目。缺软件也照样是一条练习任务（tier 降成只读、先申请）。"""
    sid = str(t.get("sid") or t.get("title"))[:40]
    need = [str(x) for x in (t.get("needs_software") or []) if str(x).strip()]
    miss = [str(x) for x in (missing if missing is not None else []) if str(x).strip()]
    base_verify = t.get("verify") or (
        f"动手做完「{t.get('title')}」后写进 self/practice/log.md：做了什么、结果、"
        f"可复现命令或产物路径；解决的错误数 / 还没解决的错误数各写几个；"
        f"做不成的写清卡在哪一步并附报错原文，不许空手结项")
    return {
        "id": ID_PREFIX + sid,
        "kind": KIND,
        "title": (f"练习：{t.get('title')}" if not miss
                  else f"练习：{t.get('title')}（缺软件 {'/'.join(miss)}，先申请）"),
        "keyword": t.get("title"),
        "priority": int(t.get("priority") or 4),
        "cost_rmb": 0.0,
        "source": "自迭代练习池",
        "source_url": "",
        "ttl_h": TTL_H,
        "tier": "t2_write_low" if not miss else "t1_readonly",
        "ask_owner": bool(miss),
        "added_at": now_str(),
        "sandbox": t.get("sandbox") or "新工程 / 独立目录，不碰已有工程与设置",
        "needs_software": need,
        "why": t.get("why") or "",
        "verify": base_verify if not miss else (
            f"缺 {'/'.join(miss)}：先问主人要不要装（不许自己下装）；"
            f"装好了再按原要求做，不装就说明卡在缺软件、记一条未解决。\n" + base_verify),
    }


def req_of(t: dict, soft: str) -> dict:
    """缺软件 → 只生成「申请安装」条目，绝不自己下装（L1：装软件属需许可的写动作）。"""
    sid = str(t.get("sid") or t.get("title"))[:40]
    return {
        "id": REQ_PREFIX + sid + "_" + soft,
        "kind": KIND,
        "title": f"练习缺软件：申请安装 {soft}（做「{t.get('title')}」要用）",
        "keyword": soft,
        "priority": 6,
        "cost_rmb": 0.0,
        "source": "自迭代练习池",
        "ttl_h": TTL_H,
        "tier": "t1_readonly",
        "added_at": now_str(),
        "ask_owner": True,
        "sandbox": "只申请，不下载不安装；等主人批准后再谈装哪版、装哪",
        "needs_software": [soft],
        "verify": f"问主人是否安装 {soft}：要装就等主人自己装或明确授权；不装就换一条不依赖它的练习",
    }


def _finished(sid: str) -> bool:
    """练完 = 评过分，且最近一次剩余未解决为 0（主人定的退出条件）。"""
    if not sid or not SCORE.exists():
        return False
    try:
        d = json.loads(SCORE.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    st = (d.get("by_task") or {}).get(sid)
    if not isinstance(st, dict):
        return False
    return (int(st.get("rounds") or 0) > 0
            and int(st.get("last_open", st.get("open") or 0)) == 0)


def fill(n: int = 0) -> dict:
    """把冷库里「还没练完」的任务全排进池；缺软件的、有软件的都在池里排轮询。

    2026-11-17 主人改口径，旧实现的两个病都在这：
      · 旧 `need = max(0, n-len(live))`：窗口满 3 条后 need=0，整个冷库扫描被跳过，
        缺软件任务永远生成不出申请（--add --need 只回 added:0）；
      · 旧 `req_` 条目只进不出：软件装好了还赖在池里，只能 --reset 手动清。
    现在每次按冷库重排：软件状态现查现判，装好了那条自动从「申请」变成正式练习。
    n>0 时只限制「本次新增几条」，池里已有的不会被漏掉。
    """
    lib = load_lib()
    pool = load_pool()
    old_items = [it for it in (pool.get("items") or []) if isinstance(it, dict) and it.get("id")]
    old = {str(it["id"]): it for it in old_items}
    cap = int(n) if n and int(n) > 0 else 0

    out, added, waiting, finished = [], 0, [], 0
    for t in sorted(lib.get("tasks") or [], key=lambda x: -int(x.get("priority") or 0)):
        sid = str(t.get("sid") or t.get("title"))
        if _finished(sid):
            finished += 1
            continue                                   # 练完了，退出轮询
        missing = [str(x) for x in (t.get("needs_software") or [])
                   if str(x).strip() and not software_ok(x)]
        it = item_of(t, missing)
        prev = old.get(it["id"])
        if prev:                                       # 已在池：保 added_at（别把轮询顺序洗掉）
            it["added_at"] = prev.get("added_at") or it["added_at"]
            if prev.get("status"):
                it["status"] = prev["status"]
            out.append(it)
        elif cap and added >= cap:
            continue
        else:
            out.append(it)
            added += 1
        if missing:
            waiting.append(it["id"])

    # 旧的 req_ 申请条目一律清掉：正式条目已按当前软件状态重新生成，不再两套并存
    dropped = [i for i in old if i.startswith(REQ_PREFIX) and i not in {x["id"] for x in out}]

    pool["items"] = out
    pool["generated_at"] = pool.get("generated_at") or now_str()
    pool["updated_at"] = now_str()
    pool["rule"] = "缺软件/有软件都入池，按池轮询；做过一次只冷却到下一轮，练完（未解决归零）才退出"
    POOL.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")

    total = len(lib.get("tasks") or [])
    st = {
        "ts_unix": time.time(), "ts": now_str(),
        "lib_total": total,
        "window": len(out), "added": added,
        "waiting_software": waiting,
        "dropped_req": dropped,
        "queue_title": (out[0]["title"] if out else None),
    }
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"added": added, "window": len(out), "waiting_software": len(waiting),
            "left": max(0, total - finished - len(out)), "total": total}


# ---------------------------------------------------------------- 评分
def record_score(sid: str, resolved: int, open_: int, note: str = "") -> dict:
    """练手评分：解决越多越高，未解决越多越低。score = Σ解决 − Σ未解决。"""
    resolved, open_ = int(resolved), int(open_)
    d = {}
    if SCORE.exists():
        try:
            d = json.loads(SCORE.read_text(encoding="utf-8-sig"))
        except Exception:
            d = {}
    by = d.get("by_task") or {}
    cur = by.get(sid) or {"resolved": 0, "open": 0, "rounds": 0}
    cur["resolved"] += resolved
    cur["open"] += open_
    cur["rounds"] += 1
    cur["last_ts"] = now_str()
    cur["last_open"] = open_          # 最近一次剩余未解决（练完=0）
    cur["last_note"] = note[:200]
    by[sid] = cur
    tot_r = sum(v.get("resolved", 0) for v in by.values())
    tot_o = sum(v.get("open", 0) for v in by.values())
    d = {
        "updated_at": now_str(),
        "rule": "score = 累计解决 − 累计未解决；另给解决率。分数只做记录，不参与选事排序。",
        "total_resolved": tot_r,
        "total_open": tot_o,
        "score": tot_r - tot_o,
        "resolve_rate": round(tot_r / (tot_r + tot_o), 3) if (tot_r + tot_o) else None,
        "by_task": by,
    }
    SCORE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n- {now_str()} 练习评分 `{sid}`：解决 {resolved}、未解决 {open_}，"
                    f"总分 {d['score']}（解决率 {d['resolve_rate']}）{('；' + note) if note else ''}\n")
    except Exception:
        pass
    return d


def show() -> None:
    lib = load_lib()
    pool = load_pool()
    prac = [it for it in (pool.get("items") or []) if isinstance(it, dict)]
    done = {i for i in consumed_ids() if i.startswith(ID_PREFIX)}
    print(f"冷库 {len(lib.get('tasks') or [])} 条｜已练习 {len(done)}｜池内 {len(prac)} 条")
    for i, it in enumerate(prac, 1):
        flag = " ⏳缺软件" if it.get("ask_owner") else ""
        print(f"  {i}. [p{it.get('priority')}]{flag} {it.get('title')}")
        print(f"     沙箱: {str(it.get('sandbox'))[:70]}")
    if SCORE.exists():
        d = json.loads(SCORE.read_text(encoding="utf-8-sig"))
        print(f"评分: 总分 {d.get('score')}｜解决 {d.get('total_resolved')}｜"
              f"未解决 {d.get('total_open')}｜解决率 {d.get('resolve_rate')}")


def reset() -> None:
    pool = load_pool()
    pool["items"] = []
    POOL.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    print("练习池已清空（冷库没动）")


def add(title: str, priority: int = 4, need: str = "") -> None:
    lib = load_lib()
    # 用带微秒的戳：同一秒里连着 --add 两条会撞 sid，撞了就等于同一道练习（11-17 实测）
    sid = "custom_" + datetime.now(CST).strftime("%H%M%S%f")[:9]
    lib["tasks"].append({
        "sid": sid, "title": title, "priority": int(priority), "why": "主人临时加",
        "sandbox": "新工程 / 独立目录，不碰已有工程与设置",
        "needs_software": [need] if need else [],
        "errors": {"resolved": 0, "open": 0},
        "verify": (f"动手做完「{title}」后写进 self/practice/log.md：做了什么、结果、"
                   f"可复现命令或产物路径；解决的错误数 / 还没解决的错误数各写几个；"
                   f"做不成的写清卡在哪一步并附报错原文"),
    })
    save_lib(lib)
    print(f"+ 已入冷库 {ID_PREFIX}{sid}：{title}" + (f"（需 {need}）" if need else ""))
    print(json.dumps(fill(), ensure_ascii=False))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--show" in args:
        show()
    elif "--reset" in args:
        reset()
    elif "--score" in args:
        i = args.index("--score")
        try:
            sid, r, o = args[i + 1], args[i + 2], args[i + 3]
        except IndexError:
            raise SystemExit("用法：--score <sid> <解决数> <未解决数> [备注]")
        note = " ".join(args[i + 4:]) if len(args) > i + 4 else ""
        print(json.dumps(record_score(sid, r, o, note), ensure_ascii=False, indent=1))
    elif "--add" in args:
        i = args.index("--add")
        title = args[i + 1] if len(args) > i + 1 else ""
        if not title:
            raise SystemExit("--add 后面跟标题")
        need = ""
        if "--need" in args:
            j = args.index("--need")
            need = args[j + 1] if len(args) > j + 1 else ""
        add(title, need=need)
    else:
        n = 0                      # 0 = 冷库全排；旧默认 3 条窗口会把缺软件申请永久挡死
        if "--n" in args:
            n = int(args[args.index("--n") + 1])
        print(json.dumps(fill(n), ensure_ascii=False))
