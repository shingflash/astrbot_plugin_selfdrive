# -*- coding: utf-8 -*-
"""perf_audit.py — 本地硬件压力体检（只读，不改任何东西）。

为什么不用 psutil：这台机器的真解释器里没装，装包要走网络还多一个依赖。
这里直接用 ctypes 调 kernel32/psapi，一次采样 60~200ms 出结果，够快。

它回答三件事：
  1. 现在到底谁在吃 CPU/内存 —— 分「我的」和「主人的」，主人的只报不动
  2. Edge 的磁盘缓存/进程数有多少浪费 —— 交易页是我在保活的，缓存会一直长
  3. 有没有需要动手的地方 —— 输出成可执行清单，危险动作一律只建议不执行

用法：
  python perf_audit.py            # 人类可读
  python perf_audit.py --json     # 机器可读（给 iter_cycle / 汇报用）
  python perf_audit.py --cache    # 只算 Edge 缓存体积
"""
from __future__ import annotations

import argparse
import ctypes
import json
import time
from ctypes import wintypes
from pathlib import Path

SELF = Path(__file__).resolve().parent
WORK = SELF.parent

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

SIZE_T = ctypes.c_size_t
PROCESS_QUERY_LIMITED = 0x1000


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", SIZE_T), ("WorkingSetSize", SIZE_T),
                ("QuotaPeakPagedPoolUsage", SIZE_T), ("QuotaPagedPoolUsage", SIZE_T),
                ("QuotaPeakNonPagedPoolUsage", SIZE_T), ("QuotaNonPagedPoolUsage", SIZE_T),
                ("PagefileUsage", SIZE_T), ("PeakPagefileUsage", SIZE_T)]


def mem_status() -> dict:
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    k32.GlobalMemoryStatusEx(ctypes.byref(m))
    gb = 1024 ** 3
    return {
        "total_gb": round(m.ullTotalPhys / gb, 1),
        "avail_gb": round(m.ullAvailPhys / gb, 1),
        "used_pct": m.dwMemoryLoad,
        "pagefile_used_gb": round((m.ullTotalPageFile - m.ullAvailPageFile) / gb, 1),
    }


def _times(h) -> tuple[int, int]:
    c, e, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return 0, 0
    f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
    return f(k) + f(u), f(c)


def _name(h) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    n = wintypes.DWORD(1024)
    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
        return buf.value
    return ""


def snapshot() -> dict[int, dict]:
    procs: list[int] = []
    arr = (wintypes.DWORD * 4096)()
    need = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(need)):
        return {}
    n = need.value // ctypes.sizeof(wintypes.DWORD)
    out = {}
    for i in range(n):
        pid = arr[i]
        if pid <= 4:
            continue
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            continue
        try:
            t, created = _times(h)
            pm = PMC()
            pm.cb = ctypes.sizeof(pm)
            ws = 0
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(pm), ctypes.sizeof(pm)):
                ws = pm.WorkingSetSize
            out[pid] = {"cpu_100ns": t, "ws": ws, "path": _name(h)}
        finally:
            k32.CloseHandle(h)
    return out


def cpu_count() -> int:
    return k32.GetActiveProcessorCount(0xFFFF) or 1


def classify(path: str) -> str:
    low = path.lower()
    if "edge-profile" in low or "edge-astrbot" in low or "msedge" in low:
        return "edge"
    if "\\qq.exe" in low or "qq" in Path(low).name:
        return "owner"
    if "python.exe" in low:
        return "python"
    if any(k in low for k in ("steam", "pico", "cxstudy", "nvcontainer", "wallpaper", "obs")):
        return "owner"
    return "other"


def audit(interval: float = 2.0, top: int = 10) -> dict:
    a = snapshot()
    time.sleep(interval)
    b = snapshot()
    ncpu = cpu_count()
    rows = []
    for pid, cur in b.items():
        old = a.get(pid)
        if not old:
            continue
        dt = cur["cpu_100ns"] - old["cpu_100ns"]
        pct_total = dt / (interval * 1e7) * 100          # 占整机
        pct_core = dt / (interval * 1e7) * 100 * ncpu    # 占单核
        if cur["ws"] < 20 * 1024 * 1024 and pct_total < 0.3:
            continue
        rows.append({
            "pid": pid,
            "exe": Path(cur["path"]).name or "?",
            "path": cur["path"],
            "group": classify(cur["path"]),
            "cpu_pct_total": round(pct_total, 2),
            "cpu_pct_core": round(pct_core, 1),
            "ws_mb": round(cur["ws"] / 1024 / 1024, 1),
        })
    rows.sort(key=lambda r: -r["cpu_pct_total"])

    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["group"], {"cpu_pct_total": 0.0, "ws_mb": 0.0, "n": 0})
        g["cpu_pct_total"] = round(g["cpu_pct_total"] + r["cpu_pct_total"], 2)
        g["ws_mb"] = round(g["ws_mb"] + r["ws_mb"], 1)
        g["n"] += 1

    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu_cores": ncpu,
        "mem": mem_status(),
        "top_cpu": rows[:top],
        "top_mem": sorted(rows, key=lambda r: -r["ws_mb"])[:8],
        "groups": groups,
    }


def dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def edge_cache() -> dict:
    out = {}
    for prof in ("edge-profile", "edge-astrbot"):
        base = WORK / prof
        if not base.exists():
            continue
        total = dir_size(base)
        sub = {}
        for name in ("Cache", "Code Cache", "GPUCache", "Service Worker", "Default"):
            p = base / "Default" / name if name != "Default" else base / "Default"
            if p.name == "Default" and p.is_dir():
                continue
            if p.exists():
                sub[name] = round(dir_size(p) / 1024 / 1024, 1)
        out[prof] = {"total_mb": round(total / 1024 / 1024, 1), "sub": sub}
    return out


def advice(rep: dict, cache: dict) -> list[str]:
    tips = []
    mem = rep["mem"]
    if mem["used_pct"] >= 85:
        tips.append(f"内存占 {mem['used_pct']}%，只剩 {mem['avail_gb']}GB：优先关掉不用的 Edge 窗口/标签，"
                    f"别去动交易页")
    edge = rep["groups"].get("edge") or {}
    if edge.get("cpu_pct_total", 0) > 8:
        tips.append(f"Edge 一组吃了 {edge['cpu_pct_total']}% CPU（{edge.get('n', 0)} 个进程）："
                    f"大头是 OKX 交易页的行情轮询；能降的是页面脚本的刷新间隔，"
                    f"但这属于交易系统，改之前得主人点头")
    py = rep["groups"].get("python") or {}
    if py.get("ws_mb", 0) > 3000:
        tips.append(f"python 一组占 {py['ws_mb']}MB：AstrBot 本体长跑会慢慢涨内存，"
                    f"建议挑凌晨空窗重启一次（重启会短暂掉线，不自己动）")
    tot_cache = sum(v["total_mb"] for v in cache.values())
    if tot_cache > 150:
        tips.append(f"Edge 磁盘缓存共 {tot_cache:.0f}MB：清 Cache/Code Cache 能省下来，"
                    f"但要先停 Edge（会断交易页 CDP），只能挑停机窗口做")
    if not tips:
        tips.append("没看到明显浪费，保持")
    return tips


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    cache = edge_cache()
    if args.cache:
        print(json.dumps(cache, ensure_ascii=False, indent=2))
        return 0

    rep = audit(args.interval)
    if args.json:
        rep["edge_cache"] = cache
        rep["advice"] = advice(rep, cache)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    m = rep["mem"]
    print(f"[{rep['ts']}] {rep['cpu_cores']} 核｜内存 {m['total_gb']}GB，用 {m['used_pct']}%，"
          f"剩 {m['avail_gb']}GB｜页面文件已用 {m['pagefile_used_gb']}GB")
    print("\n按整机 CPU 占比：")
    print(f"  {'pid':>7}  {'进程':<16}{'分组':<8}{'整机%':>7}{'单核%':>7}{'内存MB':>9}")
    for r in rep["top_cpu"]:
        print(f"  {r['pid']:>7}  {r['exe']:<16}{r['group']:<8}"
              f"{r['cpu_pct_total']:>7}{r['cpu_pct_core']:>7}{r['ws_mb']:>9}")
    print("\n按内存：")
    for r in rep["top_mem"]:
        print(f"  {r['pid']:>7}  {r['exe']:<16}{r['group']:<8}{r['ws_mb']:>9}MB")
    print("\n分组合计：")
    for g, v in sorted(rep["groups"].items(), key=lambda x: -x[1]["cpu_pct_total"]):
        print(f"  {g:<8} {v['n']:>3} 进程  CPU {v['cpu_pct_total']:>6}%  内存 {v['ws_mb']:>8}MB")
    print("\nEdge 磁盘缓存：")
    for k, v in cache.items():
        print(f"  {k}: {v['total_mb']}MB  {v['sub']}")
    print("\n建议：")
    for t in advice(rep, cache):
        print(f"  - {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
