# -*- coding: utf-8 -*-
"""把 bio_signals.json 从「我手填」改成「脚本测」。

五条信号的可测化（2026-11-08 补两路：受伤类 / 容量类）：
  rejection_count : 最近 15 枚 decision 里 pick=null（stall/skip）的占比 —— 空转率，可复算
  thermal_runtime : CPU / 内存压力合成（阈值写死，不许改口），uptime 只作参考不参与
  failure_count   : 最近 N 小时系统层受伤事件（应用崩溃 1000/1001/1026、意外关机 41/6008）
  capacity        : 磁盘可用率 + 页面文件/交换压力的最坏值
  self_pain       : 无客观来源，保持 null（不编）

原则（写死，不许在会话里改口）：
  1. 未观测就写 null，参与加权时被跳过 —— 绝不用 0 顶替，0 是「观测到没病」。
  2. 只采「能预测下一轮干不成事」的东西；CPU 高低是负载不是痛，别往里塞。
  3. 采集本身别变成负载：单次 <6s，五分钟级别频率。
  4. 评分函数是纯函数（score_*），测试喂 fixture，不联网不依赖真机状态。
"""
import json
import subprocess
import time
import psutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

SELF = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))

CPU_TH = [(80, 0.6), (60, 0.3)]      # cpu% >= 阈值 → 计分
MEM_TH = [(90, 0.4), (80, 0.2)]
DECISION_WINDOW = 15

# 受伤类：崩溃/意外关机。Windows 事件里这几条是「真出事」而不是「吵」。
CRASH_IDS = {1000, 1001, 1026, 1002}     # 应用崩溃 / WER / .NET 未处理异常 / 应用挂起
KILL_IDS = {41, 6008, 1001}              # 意外关机 / 非正常断电
FAILURE_TH = [(6, 1.0), (3, 0.6), (1, 0.3)]   # (条数下限, 分值)
FAILURE_WINDOW_H = 6

# 容量类：写不进去比跑得慢更致命
# 2026-11-08 真机校准：只看比例会把「14.5% 但有 65G 空闲」判成紧张 —— 比例和绝对量必须同时不够才算病。
DISK_TH = [(0.03, 10, 1.0), (0.08, 20, 0.6), (0.15, 40, 0.3)]   # (可用率<, 且空闲<GB, 分值)
SWAP_TH = [(0.90, 0.6), (0.75, 0.3)]


def now():
    return datetime.now(CST)


def _voluntary_skip(dec: dict) -> bool:
    """主动不干（工资池见底 / 闸门停 / 停机待审 / 钱还没攒够）不算被拒。

    那是自己踩的刹车或者口袋不够深，不是外面给的脸色；
    算进 rejection_count 会让痛觉自己把自己顶死。
    """
    if dec.get("pick"):
        return False
    if dec.get("blocked"):
        return True
    # 旧格式兼容（2026-11-02 18:20 之前的 decision 没有 blocked 字段）
    return (dec.get("quota_state") in ("skip", "empty")
            or dec.get("gate") == "stop"
            or dec.get("level") == "halt")


def idle_ratio() -> tuple[float, dict]:
    d = SELF / "iter_out"
    files = sorted(d.glob("decision_*.json"), reverse=True)[:DECISION_WINDOW] if d.exists() else []
    total = hit = voluntary = 0
    for f in files:
        try:
            dec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        total += 1
        if not dec.get("pick"):
            if _voluntary_skip(dec):
                voluntary += 1
            else:
                hit += 1
    return (round(hit / total, 3) if total else 0.0), {
        "rounds": total, "idle": hit, "voluntary": voluntary
    }


def thermal(samples: int = 3, gap: float = 0.5) -> tuple[float, dict]:
    """取 N 次 CPU 采样均值 —— 单点采样在十分钟内能从 62% 翻到 39%，定不了生死。"""
    psutil.cpu_percent(interval=None)   # 打点，丢弃
    time.sleep(1.0)
    cpu_s = []
    for i in range(samples):
        cpu_s.append(psutil.cpu_percent(interval=None))
        if i < samples - 1:
            time.sleep(gap)
    cpu = round(sum(cpu_s) / len(cpu_s), 1)
    mem = psutil.virtual_memory().percent
    score = 0.0
    for th, v in CPU_TH:
        if cpu >= th:
            score += v
            break
    for th, v in MEM_TH:
        if mem >= th:
            score += v
            break
    return round(min(1.0, score), 3), {"cpu": cpu, "mem": mem, "samples": cpu_s}


# ---------------------------------------------------------------- 受伤类

def score_failures(events: list[dict]) -> tuple[float, dict]:
    """events: [{"id": 1000, "provider": "Application Error", "kind": "crash"|"kill"}, ...]

    2026-11-08 真机校准：第一版按条数分档，结果 6 小时里 18 条全是 usbperf 的 2001
    反复刷屏 → 直接顶到 1.0。**同一个 source 反复吵不是伤，是新伤口的数量才算伤。**
    所以改成按「不同来源（provider）数」分档，条数只作展示。

    规则：
      - 出现「意外关机/非正常断电」(kill) 一次即 1.0 —— 这是重伤，不是普通报错。
      - 其余按 distinct provider 数：≥6→1.0 / ≥3→0.6 / ≥1→0.3 / 0→0.0
    """
    evs = [e for e in events if isinstance(e, dict) and int(e.get("id") or 0) > 0]
    ids = [int(e["id"]) for e in evs]
    if any(i in KILL_IDS for i in ids):
        return 1.0, {"events": len(ids), "kill": [i for i in ids if i in KILL_IDS][:3], "note": "意外关机/断电"}
    srcs = sorted({(e.get("provider") or f"id:{e['id']}") for e in evs})
    n = len(srcs)
    score = 0.0
    for th, v in FAILURE_TH:
        if n >= th:
            score = v
            break
    return round(min(1.0, score), 3), {"events": len(ids), "sources": srcs[:6], "ids": sorted(set(ids))[:8]}


_PS_FAIL = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
function Emit($evts) { foreach ($e in $evts) { "{0}|{1}" -f $e.Id, $e.ProviderName } }
try { Emit (Get-WinEvent -FilterHashtable @{LogName='Application';Level=1,2;StartTime=(Get-Date).AddHours(-%d)} -ErrorAction Stop) } catch {}
try { Emit (Get-WinEvent -FilterHashtable @{LogName='System';Id=41,6008;StartTime=(Get-Date).AddHours(-%d)} -ErrorAction Stop) } catch {}
"""


def collect_failures(hours: int = FAILURE_WINDOW_H) -> tuple:
    """跑 PowerShell 拿 "id|provider" 行；超时/报错一律 None（未观测），不写 0。"""
    script = _PS_FAIL % (hours, hours)
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=20,
        )
        out = cp.stdout.decode("utf-8", "replace")
    except Exception as e:
        return None, {"error": type(e).__name__}
    events = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        a, b = line.split("|", 1)
        if a.strip().isdigit():
            events.append({"id": int(a.strip()), "provider": b.strip()})
    return score_failures(events)


# ---------------------------------------------------------------- 容量类

def score_capacity(disks: list[dict], swap_pct: float | None) -> tuple[float, dict]:
    """disks: [{"mount": "C:\\", "free_ratio": 0.07, "free_gb": 12.3}, ...]

    取两路里最坏的：磁盘可用率越低越痛（写不进日志/记忆就是废人），
    页面文件压力高说明在拿硬盘当内存。
    """
    worst = 0.0
    meta = {"disks": []}
    for d in disks or []:
        fr = d.get("free_ratio")
        if not isinstance(fr, (int, float)):
            continue
        fg = d.get("free_gb")
        meta["disks"].append({"mount": d.get("mount"), "free_ratio": round(fr, 4), "free_gb": round(fg or 0, 1)})
        for th_r, th_gb, v in DISK_TH:
            if fr < th_r and isinstance(fg, (int, float)) and fg < th_gb:
                worst = max(worst, v)
                break
    if isinstance(swap_pct, (int, float)):
        meta["swap_pct"] = round(swap_pct, 1)
        for th, v in SWAP_TH:
            if swap_pct / 100.0 >= th:
                worst = max(worst, v)
                break
    else:
        meta["swap_pct"] = None
    return round(min(1.0, worst), 3), meta


def collect_capacity() -> tuple:
    disks = []
    try:
        for p in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            if not u.total:
                continue
            disks.append({"mount": p.mountpoint, "free_ratio": u.free / u.total, "free_gb": u.free / (1024 ** 3)})
    except Exception as e:
        return None, {"error": type(e).__name__}
    swap_pct = None
    try:
        sw = psutil.swap_memory()
        if sw.total:
            swap_pct = sw.percent
    except Exception:
        pass
    if not disks:
        return None, {"error": "no-disk"}
    return score_capacity(disks, swap_pct)


# ---------------------------------------------------------------- main

def main():
    rej, rej_meta = idle_ratio()
    th, th_meta = thermal()
    fail, fail_meta = collect_failures()
    cap, cap_meta = collect_capacity()
    up_h = round((time.time() - psutil.boot_time()) / 3600, 2)

    payload = {
        "_note": "由 self/collect_signals.py 采写，别手填。null = 未观测，不参与加权。",
        "_collected_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        "self_pain": None,
        "balance_burn": None,
        "rejection_count": rej,
        "thermal_runtime": th,
        "failure_count": fail,
        "capacity": cap,
        "_source": {
            "rejection_count": (f"最近 {rej_meta['rounds']} 轮里 {rej_meta['idle']} 轮真空转"
                                f"（另有 {rej_meta['voluntary']} 轮配额/闸门主动跳过，不计）"),
            "thermal_runtime": f"cpu={th_meta['cpu']}%（样本 {th_meta['samples']}）mem={th_meta['mem']}%（阈值写死在脚本里）",
            "failure_count": (f"最近 {FAILURE_WINDOW_H}h 事件 {fail_meta.get('events', '?')} 条 / "
                              f"{len(fail_meta.get('sources') or [])} 个来源"
                              + (f"，含意外关机 {fail_meta.get('kill')}" if fail_meta.get("kill") else "")
                              if fail is not None else f"采集失败：{fail_meta.get('error')}，保持 null"),
            "capacity": (f"磁盘 {cap_meta.get('disks')} swap={cap_meta.get('swap_pct')}%"
                         if cap is not None else f"采集失败：{cap_meta.get('error')}，保持 null"),
            "self_pain": "无客观来源，保持 null",
            "host_uptime_h": up_h,
        },
    }
    p = SELF / "bio_signals.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{payload['_collected_at']}] rejection={rej} thermal={th} failure={fail} capacity={cap} uptime={up_h}h")
    for k, v in payload["_source"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
