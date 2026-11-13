#!/usr/bin/env python3
"""自迭代一圈（不烧 LLM 的那部分）。

读外置信号 → 算偏差 → 从 goals.json 挑一件事 → 结论写回 log.md。
脚本自己不下判断、不调模型，只产出"这轮该干什么"的建议；
真正动手的是被唤醒的我，动作按 priority.py 定性。

用法：
    python self/iter_cycle.py                 # 只算，打印建议，不写文件
    python self/iter_cycle.py --commit        # 写 decision 文件 + 追加 log.md
    python self/iter_cycle.py --json          # 机器可读
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SELF = Path(__file__).resolve().parent
BASE = SELF.parent
CST = timezone(timedelta(hours=8))

LEVELS = ("normal", "throttle", "readonly", "halt")
# 段位只约束「一轮能烧多少」，不再兼任日配额；真上限由钱包余额和 SINGLE_ROUND_CAP_RMB 卡
COST_CEILING = {"normal": 5.0, "throttle": 2.0, "readonly": 0.0, "halt": 0.0}
STALE_STEP = 2.0   # 每有一轮没被选中，该目标上浮的分
STALE_CAP = 10.0   # 上浮封顶，别让低优先级目标一路顶到压过真实排序


def now() -> datetime:
    return datetime.now(CST)


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def last_log_ts(p: Path) -> datetime | None:
    """log.md 里形如 `2026-11-01 19:30 | 动作 | ...` 的最后时间。"""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        m = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)", line.strip())
        if m:
            s = m.group(1).replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(s, fmt).replace(tzinfo=CST)
                except ValueError:
                    continue
    return None


PROBE_PORTS = [("127.0.0.1", 6199), ("127.0.0.1", 6185)]
WEIGHTS = {"process_health": 20, "self_pain": 20, "balance_burn": 20,
           "rejection_count": 20, "thermal_runtime": 5, "failure_count": 10, "capacity": 5}
# 2026-11-08 23:18 主人给口令后接线：failure_count(受伤10) / capacity(容量5) 进门，
# balance_burn 与 rejection_count 各让 5，thermal_runtime 10→5（它是负载不是痛）。
# self_pain 20 保留占位：恒 null 不进分母，不摊薄。
# 价值层加分（主人 11-05 00:36 定序：有价值 > 优化 > 验证 > 空转）。
# maintain 拿 0 分且另有 -10 惩罚，等于「验证是最后手段」；archive 只是留痕，给一点点。
VALUE_BONUS = {"explore": 12, "curiosity": 10, "inspiration": 10, "meta": 8, "optimize": 8, "archive": 2}
MAINTAIN_PENALTY = 10.0


def _port_alive(host: str, port: int, tries: int = 2, timeout: float = 1.2) -> bool:
    """连续 tries 次都不通才算死 —— llbot 重启那几秒的瞬断不该被记成我生病。"""
    import socket

    for i in range(tries):
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except Exception:
            if i < tries - 1:
                time.sleep(0.3)
    return False


def probe_ports(targets=PROBE_PORTS) -> float:
    """进程健康：端口连续探测都失败才计事故，返回 0~1 的坏度。"""
    down = 0
    for host, port in targets:
        if not _port_alive(host, int(port)):
            down += 1
    return min(1.0, down / max(1, len(targets)))


SIGNAL_MAX_AGE_H = 24.0


def _signals_fresh(ext: dict) -> bool:
    """外部信号必须带 _collected_at 且在 SIGNAL_MAX_AGE_H 小时内，否则视为未观测。

    没有时间戳的数字等于没测 —— 陈旧值当真会让痛觉变成化石。
    """
    ts = str(ext.get("_collected_at") or "")[:19]
    if not ts:
        return False
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
    except Exception:
        return False
    return (now() - dt).total_seconds() <= SIGNAL_MAX_AGE_H * 3600


def compute_pain(decay_per_hour: float = 0.5) -> tuple[float, dict]:
    """自己量、自己算、不回嘴：探针 + 外部 JSON → 0~100，带时间衰减。

    0.5 分/小时：36 分的痛要 72 小时无事故才归零，别指望睡一觉就自愈。
    """
    h = read_json(SELF / "health.json", {}) or {}
    ext = read_json(SELF / "bio_signals.json", {}) or {}
    fresh = _signals_fresh(ext)
    sig = {"process_health": probe_ports()}
    for k in ("self_pain", "balance_burn", "rejection_count", "thermal_runtime",
              "failure_count", "capacity"):
        if fresh and isinstance(ext.get(k), (int, float)):
            sig[k] = max(0.0, min(1.0, float(ext[k])))
    if "balance_burn" not in sig:
        sig["balance_burn"] = burn_signal()
    total_w = sum(WEIGHTS.get(k, 0) for k in sig) or 1
    pain = sum(sig[k] * WEIGHTS.get(k, 0) for k in sig) / total_w * 100
    prev = h.get("pain")
    upd = h.get("updated_at")
    if isinstance(prev, (int, float)) and upd:
        try:
            dt = datetime.strptime(upd, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            hours = max(0.0, (now() - dt).total_seconds() / 3600)
            pain = max(pain, float(prev) - decay_per_hour * hours)
        except Exception:
            pass
    meta = {k: round(v, 3) for k, v in sig.items()}
    if ext and not fresh:
        meta["stale"] = 1
    return round(min(100.0, max(0.0, pain)), 1), meta


def write_health(pain: float, signals: dict):
    p = SELF / "health.json"
    h = read_json(p, {}) or {}
    h["pain"] = pain
    h["signals"] = {k: {"value": v} for k, v in signals.items() if k != "stale"}
    h["updated_at"] = now().strftime("%Y-%m-%d %H:%M:%S")
    p.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")


def read_pain() -> tuple[float, dict, str]:
    """痛值只认 self/health.json（biofeedback 插件已删，死分支 2026-11-02 清掉）。"""
    p = SELF / "health.json"
    d = read_json(p, None)
    if isinstance(d, dict) and isinstance(d.get("pain"), (int, float)):
        return float(d["pain"]), d.get("signals") or {}, str(p)
    return 0.0, {}, "none"


def level_of(pain: float, th: dict | None = None) -> str:
    th = th or {"throttle": 30, "readonly": 60, "halt": 85}
    if pain >= float(th.get("halt", 85)):
        return "halt"
    if pain >= float(th.get("readonly", 60)):
        return "readonly"
    if pain >= float(th.get("throttle", 30)):
        return "throttle"
    return "normal"


def _ledger():
    if str(SELF) not in sys.path:
        sys.path.insert(0, str(SELF))
    import ledger as _l  # 本地零依赖移植版

    return _l


def read_budget() -> dict:
    """已退役（2026-11-02 18:20 起改工资池 wallet.json）。留着只为读旧字段做展示。"""
    """今日自配额（主人给的 1 rmb 怎么切），没有就返回空。"""
    return read_json(SELF / "budget.json", {}) or {}


def read_balance_rmb() -> float | None:
    """读 DSH 账本（~/.dsh/.dshw-usage.json），口径照 accounting.mjs，不用开客户端。

    余额一律走账本本位币（人民币），不做汇率折算。
    """
    env = os.getenv("BIO_BALANCE_RMB")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        return float(_ledger().snapshot()["balance"])
    except Exception:
        pass
    for q in (SELF / "bio_signals.json", SELF / "balance.json"):
        e = read_json(q, {})
        for k in ("balance_rmb", "balance"):
            if isinstance(e.get(k), (int, float)):
                return float(e[k])
    return None


def burn_signal() -> float:
    """今日已观测消费 / 近几日日均：等于日均记 0，超 3 倍记满。"""
    try:
        _, _, ratio = _ledger().burn_ratio()
    except Exception:
        return 0.0
    if ratio <= 1:
        return 0.0
    return max(0.0, min(1.0, (ratio - 1) / 2))


# ---------------- 工资池（2026-11-02 起：每天 00:00 发 1rmb，可攒，不许透支） ----------------
PAYROLL_RMB = float(os.getenv("PAYROLL_RMB", "1.0"))
MAX_BACKPAY_DAYS = 7          # 断线回来最多补 7 天，关一个月不会暴富
SINGLE_ROUND_CAP_RMB = 5.0    # 一轮最多花这么多，再大必须走 t4 口令
WALLET = SELF / "wallet.json"
WALLET_LOCK = SELF / "wallet.lock"
_LOCK_TIMEOUT = 15.0


@contextlib.contextmanager
def wallet_lock():
    """跨进程互斥。wallet 是典型的读-改-写，两个进程同时进 payroll / wallet_spend
    会各加各的再互相覆盖，盘上只落一份余额、ledger 却留两条 delta。"""
    fh = None
    deadline = time.time() + _LOCK_TIMEOUT
    try:
        fh = open(WALLET_LOCK, "a+b")
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f"wallet.lock 等锁超时 {_LOCK_TIMEOUT}s")
                time.sleep(0.05)
        yield
    finally:
        if fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def wallet_load() -> dict:
    w = read_json(WALLET, {}) or {}
    if not isinstance(w.get("balance_rmb"), (int, float)):
        w = {
            "balance_rmb": 0.0,
            "last_payroll": now().strftime("%Y-%m-%d"),
            "ledger": [],
            "note": "每天 00:00 发 PAYROLL_RMB，可攒着做大事；不许透支，一轮上限 SINGLE_ROUND_CAP_RMB。",
        }
    w.setdefault("ledger", [])
    return w


def wallet_save(w: dict):
    w["updated"] = now().strftime("%Y-%m-%d %H:%M:%S")
    WALLET.write_text(json.dumps(w, ensure_ascii=False, indent=2), encoding="utf-8")


def payroll() -> tuple[float, int]:
    """结算到今日：缺几天补几天（封顶 MAX_BACKPAY_DAYS），返回 (余额, 本次补发天数)。"""
    with wallet_lock():
        w = wallet_load()
        today = now().date()
        try:
            last = datetime.strptime(str(w.get("last_payroll")), "%Y-%m-%d").date()
        except Exception:
            last = today
        days = (today - last).days
        if days <= 0:
            return float(w["balance_rmb"]), 0
        capped = min(days, MAX_BACKPAY_DAYS)
        amount = round(capped * PAYROLL_RMB, 3)
        w["balance_rmb"] = round(float(w["balance_rmb"]) + amount, 3)
        w["last_payroll"] = today.strftime("%Y-%m-%d")
        w["ledger"] = (w.get("ledger") or [])[-99:] + [{
            "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
            "delta": amount,
            "reason": f"日薪×{capped}" + (f"（断线 {days} 天，封顶 {MAX_BACKPAY_DAYS}）" if days > capped else ""),
            "balance": w["balance_rmb"],
        }]
        wallet_save(w)
        return float(w["balance_rmb"]), capped


def wallet_spend(cost: float, reason: str = "") -> bool:
    """从余额扣；不够就不扣（不许透支），返回是否扣成功。"""
    cost = round(float(cost or 0), 3)
    if cost <= 0:
        return True
    with wallet_lock():
        w = wallet_load()
        if float(w["balance_rmb"]) + 1e-9 < cost:
            return False
        w["balance_rmb"] = round(float(w["balance_rmb"]) - cost, 3)
        w["ledger"] = (w.get("ledger") or [])[-99:] + [{
            "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
            "delta": -cost, "reason": (reason or "自迭代一轮")[:40],
            "balance": w["balance_rmb"],
        }]
        wallet_save(w)
        return True


def wallet_state() -> tuple[str, float]:
    bal = round(float(wallet_load()["balance_rmb"]), 3)
    if bal <= 0.001:
        return "empty", bal
    if bal < PAYROLL_RMB:
        return "tight", bal
    return "ok", bal


# ---------------- 轮次真实消耗对账（2026-11-13 主人定：工资还是 1 元，账要记真钱） ----------------
ROUND_MARK = SELF / "round_balance.json"
ROUND_HISTORY = SELF / "round_costs.jsonl"


def _latest_pick() -> dict:
    """最近一枚 decision 里选中的那一项，给花费打标用。"""
    try:
        files = sorted((SELF / "iter_out").glob("decision_*.json"))
        d = read_json(files[-1], {}) if files else {}
        p = d.get("pick") if isinstance(d, dict) else None
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def _latest_pick_kind() -> str:
    return str(_latest_pick().get("kind") or "")


def goal_cost_stats() -> dict:
    """按目标（方向）汇总真实花费：走了几轮、累计多少、单轮均值。均值只做观察，不参与决策。"""
    out: dict[str, dict] = {}
    try:
        lines = ROUND_HISTORY.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        spent = float(r.get("spent") or 0)
        if spent <= 0:
            continue
        gid = str(r.get("goal_id") or "")
        key = gid or f"(无目标·{r.get('kind') or 'unknown'})"
        e = out.setdefault(key, {"goal": key, "title": str(r.get("goal_title") or ""),
                                 "rounds": 0, "total": 0.0, "last": "", "steps": []})
        e["rounds"] += 1
        e["total"] = round(e["total"] + spent, 3)
        e["last"] = str(r.get("settled_at") or "")[:16]
        e["steps"].append(spent)
    for e in out.values():
        n = e["rounds"]
        s = sorted(e["steps"])
        e["mean"] = round(e["total"] / n, 3) if n else 0.0
        e["max"] = round(s[-1], 3) if s else 0.0
        e.pop("steps", None)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["total"]))


def cost_stats(kind: str | None = None) -> dict:
    """按 kind 汇总历史真实花费：样本数、均值、中位、P90、最大。"""
    rows = []
    try:
        for line in ROUND_HISTORY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            spent = float(r.get("spent") or 0)
            if spent <= 0:
                continue
            rows.append({"kind": str(r.get("kind") or "unknown"), "spent": spent})
    except FileNotFoundError:
        pass
    groups: dict[str, list[float]] = {}
    for r in rows:
        groups.setdefault(r["kind"], []).append(r["spent"])
    if kind:
        groups = {k: v for k, v in groups.items() if k == kind}

    def _sum(v: list[float]) -> dict:
        s = sorted(v)
        n = len(s)
        return {
            "n": n,
            "mean": round(sum(s) / n, 3),
            "median": round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 3),
            "p90": round(s[min(n - 1, int(round(n * 0.9)) - 1)] if n > 1 else s[0], 3),
            "max": round(s[-1], 3),
        }

    return {k: _sum(v) for k, v in sorted(groups.items())}


def _global_cost() -> float | None:
    """不分类型的加权均值，样本够 3 条才认 —— 单类型太散时的兜底。"""
    st = cost_stats()
    n = sum(v["n"] for v in st.values())
    if n < 3 or not st:
        return None
    return round(sum(v["mean"] * v["n"] for v in st.values()) / n, 3)


def estimate_cost(goal_kind: str, fallback: float) -> float:
    """预测这类动作一轮会花多少钱：同类均值 → 全局均值 → 填的常数。"""
    st = cost_stats(goal_kind).get(goal_kind)
    if st and st.get("n", 0) >= 2:
        return float(st["mean"])
    g = _global_cost()
    if g is not None:
        return g
    return float(fallback or 0)


def cost_p90(goal_kind: str, fallback: float) -> float:
    """攒钱够不够按 P90 判，不用均值 —— 均值意味着一半概率超支。"""
    st = cost_stats(goal_kind).get(goal_kind)
    if st and st.get("n", 0) >= 2:
        return max(float(st["p90"]), float(fallback or 0))
    g = _global_cost()
    if g is not None:
        return max(round(g * 1.5, 3), float(fallback or 0))  # 兜底按 1.5 倍保守估
    return float(fallback or 0)


def round_start(label: str = "") -> dict:
    """一轮 agent 唤醒的开头：记下 DSH 账本真实余额。

    iter_cycle.py 本身是纯标准库脚本，跑它不烧 token；烧钱的是被唤醒后
    读文件、思考的我。所以这个标记要卡在整轮两端，不是脚本执行前后。
    """
    bal = read_balance_rmb()
    mark = {
        "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
        "balance": bal,
        "label": str(label or "")[:40],
        "settled": False,
    }
    ROUND_MARK.write_text(json.dumps(mark, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, **mark, "wallet": wallet_state()[1]}


def round_end(charge: bool = True) -> dict:
    """一轮结束：余额差值 = 这一轮真烧掉的 token 钱，从工资钱包扣。

    差值含同期别人的消耗（我自己回话、别的 cron），是上界不是精确值；
    充值导致差值为负按 0 算，不当退款；钱包不够就只扣到 0，不透支。
    """
    mark = read_json(ROUND_MARK, {}) or {}
    if not mark:
        return {"ok": False, "reason": "没有轮次标记，先跑 --round-start"}
    if mark.get("settled"):
        return {"ok": False, "reason": "这一轮已经结过账", "settled_at": mark.get("settled_at")}
    start_bal = mark.get("balance")
    end_bal = read_balance_rmb()
    if start_bal is None or end_bal is None:
        return {"ok": False, "reason": "账本余额读不到（DSH 没在观测），本轮不结账"}
    diff = round(float(start_bal) - float(end_bal), 4)
    if diff < 0:
        diff = 0.0
    before = wallet_state()[1]
    paid = 0.0
    if diff > 0 and charge:
        paid = min(diff, max(0.0, before))
        if paid > 0:
            wallet_spend(paid, f"真实token·{mark.get('label') or '轮次'}")
    pick = _latest_pick()
    mark.update({
        "settled": True,
        "settled_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        "end_balance": end_bal,
        "spent": diff,
        "charged": bool(diff > 0 and charge),
        "deducted": round(paid, 4),
        "wallet_before": before,
        "wallet_after": wallet_state()[1],
        "short": round(diff - paid, 4) if diff > paid else 0.0,
        "kind": str(pick.get("kind") or ""),
        "goal_id": str(pick.get("id") or ""),
    })
    ROUND_MARK.write_text(json.dumps(mark, ensure_ascii=False, indent=2), encoding="utf-8")
    # 历史流水：一行一轮，按方向累计（这个方向一共烧了多少、走了几轮）
    try:
        with ROUND_HISTORY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": mark.get("ts"),
                "settled_at": mark["settled_at"],
                "label": mark.get("label"),
                "kind": mark.get("kind") or "unknown",
                "goal_id": mark.get("goal_id") or "",
                "goal_title": str(pick.get("title") or "")[:60],
                "spent": diff,
                "deducted": round(paid, 4),
                "wallet_after": mark["wallet_after"],
                "short": mark["short"],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return {"ok": True, **mark}


# 闸门直接用账本人民币余额比，不换汇；21/35 是原 3U/5U 的口径折算
GATE_STOP_RMB = float(os.getenv("GATE_STOP_RMB", "21"))
GATE_NO_EXPLORE_RMB = float(os.getenv("GATE_NO_EXPLORE_RMB", "35"))


def gate(balance: float | None) -> tuple[str, float]:
    """低于 GATE_STOP_RMB 自主全停，低于 GATE_NO_EXPLORE_RMB 停探索；说话回话不受影响。"""
    if balance is None:
        return "unknown", 1.0
    if balance < GATE_STOP_RMB:
        return "stop", 0.0
    if balance < GATE_NO_EXPLORE_RMB:
        return "no_explore", 0.0
    return "ok", 1.0


def load_recent_decisions(limit: int = 15) -> list:
    """按时间倒序读最近若干枚 decision 文件（给 streak / 当日探索判断用）。"""
    out = []
    d = SELF / "iter_out"
    if not d.exists():
        return out
    for f in sorted(d.glob("decision_*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def stale_rounds(decisions: list, goal_ids: list, cap: int = 12) -> dict:
    """每个目标已经多少轮没被选中（从未选中的记 cap），给 pick 的「冒泡」用。"""
    out = {}
    for i, dec in enumerate(decisions):
        gid = (dec.get("pick") or {}).get("id")
        if gid and gid not in out:
            out[gid] = i
    for gid in goal_ids:
        if gid not in out:
            out[gid] = min(len(decisions), cap)
    return out


def pick_streak(decisions: list, max_scan: int = 60) -> dict:
    """从最近一枚往前数：同一个目标连着被建议了几次。
    空 pick（stall/skip/闸门主动跳过）不计也不截断 —— 那是踩刹车，不是换目标。"""
    streak = {}
    last_id = None
    scanned = 0
    for dec in decisions:
        gid = (dec.get("pick") or {}).get("id")
        if not gid:
            scanned += 1
            if scanned >= max_scan:
                break
            continue
        if last_id is None:
            last_id = gid
        if gid != last_id:
            break
        streak[gid] = streak.get(gid, 0) + 1
    return streak


def kind_streak(decisions: list) -> tuple:
    """最近连续几轮是同一种 kind，返 (kind, 次数)。
    同 id 连击由 pick_streak 管，这个管「不同 id 但同类动作」——
    evo_audit 报的「最长连续同类 4 轮(learn_skill)」就是这毛病。"""
    last = None
    n = 0
    for dec in decisions or []:   # decisions 已按时间倒序（最新在前），顺着数
        k = (dec.get("pick") or {}).get("kind")
        if not k:
            break
        if last is None:
            last = k
        elif k != last:
            break
        n += 1
    return last, n


def cat_streak(decisions: list, max_scan: int = 24) -> tuple:
    """最近连续几轮好奇吃的是同一个分类，返 (分类, 次数)。

    kind_streak 只管「同类动作」，管不住「好奇全是前沿科技」这种单类垄断——
    12 轮好奇 11 轮同分类，就是这条缺失造成的。

    max_scan 是扫描窗口：只看最近 24 条 decision。不设上限的话，几十轮前
    偶然的一串同类还挂在账上，会一直给当前打分扣分且永远不释放。
    """
    last = None
    n = 0
    scanned = 0
    for dec in decisions or []:   # 同上：decisions 是最新在前
        scanned += 1
        if scanned > max_scan:
            break
        p = dec.get("pick") or {}
        if p.get("kind") != "curiosity":
            continue
        c = p.get("curiosity_cat")
        if not c:
            continue
        if last is None:
            last = c
        elif c != last:
            break
        n += 1
    return last, n


def explored_today(decisions: list) -> bool:
    """今天是否已经建议过一次探索（新坑一天最多开一个）。"""
    today = now().strftime("%Y-%m-%d")
    for dec in decisions:
        if str(dec.get("ts", "")).startswith(today) and (dec.get("pick") or {}).get("kind") == "explore":
            return True
    return False


def self_spent_rmb():
    """今日自花：只算自迭代自己的轮次成本（self_cost.json 自记账）。

    不用账号账本的 today_spent —— 那含主人聊天和临时干活，会把这条线全天顶死。
    """
    d = read_json(SELF / "self_cost.json", {}) or {}
    if d.get("date") != now().strftime("%Y-%m-%d"):
        return 0.0
    return round(float(d.get("spent") or 0), 3)


def add_self_cost(cost: float):
    """一轮花掉多少就记多少，跨日归零。"""
    f = SELF / "self_cost.json"
    today = now().strftime("%Y-%m-%d")
    d = read_json(f, {}) or {}
    if d.get("date") != today:
        d = {"date": today, "spent": 0.0, "rounds": 0}
    d["spent"] = round(float(d.get("spent") or 0) + float(cost or 0), 3)
    d["rounds"] = int(d.get("rounds") or 0) + 1
    d["updated"] = now().strftime("%Y-%m-%d %H:%M:%S")
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def quota_state() -> tuple:
    """工资池制：ok=有余额；tight=不足一天工资；empty=见底。

    日上限已取消（当天花完或攒着都行），真约束是「不许透支」，在 pick() 里按余额卡。
    返回 (状态, 余额, 日薪参考)。
    """
    st, bal = wallet_state()
    return st, bal, PAYROLL_RMB


# ---------------- 自我优化（meta 位点，2026-11-04 主人派活加的环节） ----------------
# 走 skills/self-evolution：变异而不是重写、档案留痕、涨了才留。
META_COOLDOWN_H = 24.0        # 两轮自我优化之间至少隔这么久，防连刷同一个坑
META_MIN_CANDIDATES = 2       # 一次至少提 2 个候选变体，防「一次只提一个候选」的同质化
META_MAX_PAIN = 60.0          # pain 到 readonly 就别改自己代码了，先修身体
META_FORBIDDEN = (            # 宪法区：任何自我修改都不许碰，只能主人改
    "priority.py 定性表", "budget.json 日配额上限", "GATE_STOP_RMB",
    "PAYROLL_RMB", "SINGLE_ROUND_CAP_RMB",
)
ARCHIVE = SELF / "archive.jsonl"


def last_meta_ts(decisions: list) -> datetime | None:
    """回溯最近一次「自我优化」轮的时间，用于冷却判定。"""
    for d in reversed(decisions or []):
        p = d.get("pick") or {}
        if p.get("kind") == "meta" or d.get("meta_round"):
            ts = str(d.get("ts") or "")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(ts, fmt).replace(tzinfo=CST)
                except Exception:
                    continue
    return None


def meta_cooldown(decisions: list, hours: float = META_COOLDOWN_H) -> tuple[bool, float | None]:
    """(能不能开自我优化轮, 距上一轮多少小时)。没开过就 True/None。"""
    t = last_meta_ts(decisions)
    if t is None:
        return True, None
    h = round((now() - t).total_seconds() / 3600, 2)
    return h >= hours, h


def meta_pending_unused(max_age_h: float = 72.0) -> dict | None:
    """反向证据：上一轮 meta 改动如果没留下 used_by，说明改了没用上 —— 这轮先补用，别再开新坑。
    但只挡 max_age_h 小时：过期就放行，否则一次忘记填 used_by 会把 meta 轮永久锁死。"""
    try:
        rows = [json.loads(l) for l in ARCHIVE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return None
    metas = [r for r in rows if r.get("kind") == "meta"]
    if not metas:
        return None
    last = metas[-1]
    if last.get("used_by"):
        return None
    try:
        ts = datetime.strptime(str(last.get("ts"))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        if (now() - ts).total_seconds() > max_age_h * 3600:
            return None
    except Exception:
        return None
    return last


# ---------------- optimize 冷却 + 好奇机制（2026-11-04 主人派活加的） ----------------
OPTIMIZE_COOLDOWN_H = 36.0     # 同一条优化目标两次之间至少隔 36h，防像 g4 那样连中 7 轮
CURIOSITY_FILE = SELF / "goals_curiosity.json"
CURIOSITY_ROTATE_STAMP = SELF / "curiosity_rotate.json"   # 上一轮吃空后换轮的时间
CURIOSITY_TTL_H = 48.0         # 热点两天就凉，过期候选不入选
CURIOSITY_MAX_PAIN = 60.0      # 疼的时候别去刷热搜，先修身体
CURIOSITY_REFRESH_COOLDOWN_H = 3.0   # 池子吃空后最多每 3h 起一轮新的，防空转刷网络
CURIOSITY_SCAN_FILES = 400     # 判「这条吃过没」时回扫多少个决策文件


def last_kind_ts(decisions: list, kind: str) -> datetime | None:
    """回溯最近一次同类（optimize / curiosity）轮的时间。"""
    for d in reversed(decisions or []):
        p = d.get("pick") or {}
        if p.get("kind") == kind:
            ts = str(d.get("ts") or "")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(ts, fmt).replace(tzinfo=CST)
                except Exception:
                    continue
    return None


def optimize_cooldown(decisions: list, hours: float = OPTIMIZE_COOLDOWN_H) -> tuple[bool, float | None]:
    """(能不能开优化轮, 距上一轮多少小时)。没开过就 True/None。"""
    t = last_kind_ts(decisions, "optimize")
    if t is None:
        return True, None
    h = round((now() - t).total_seconds() / 3600, 2)
    return h >= hours, h


def _topic_key(text: str) -> str:
    """主题归一化：剥掉「热搜第N位」这类位次后缀、标点、空白。

    用来判「同一个热点」而不是「同一个候选 id」——候选 id 里带日期，
    跨天就会变，同一主题换个位次后缀就能被反复重新「发现」。
    """
    s = str(text or "").lower()
    s = re.sub(r"^[\s]*(好奇|学习|优化|验证|动作|建议)\s*[:：]", "", s)
    s = re.sub(r"[（(][^）)]*第\s*\d+\s*位[^）)]*[）)]", "", s)
    s = re.sub(r"第\s*\d+\s*位|热搜|热榜|top\s*\d+|no\.?\s*\d+", "", s, flags=re.I)
    s = re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)
    return s


def consumed_curiosity_ids(limit_files: int = CURIOSITY_SCAN_FILES) -> set:
    """扫历史决策文件，收集已经吃过的好奇 id。
    轮询靠它：吃过的跳过、顺位往下，全池吃完才换下一轮。
    只看 iter_out 的决策 JSON —— decisions 列表只留最近 15 条，不够用。"""
    d = SELF / "iter_out"
    if not d.exists():
        return set()
    ids: set[str] = set()
    for f in sorted(d.glob("decision_*.json"))[-limit_files:]:
        try:
            j = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        p = j.get("pick") or {}
        if p.get("kind") == "curiosity" and p.get("id"):
            ids.add(str(p["id"]))
    return ids


def consumed_curiosity_topics(limit_files: int = CURIOSITY_SCAN_FILES) -> set:
    """按主题（不是候选 id）收集已经吃过的好奇。

    跟 consumed_curiosity_ids 互补：id 管「这一条」，topic 管「这件事」。
    """
    d = SELF / "iter_out"
    if not d.exists():
        return set()
    keys: set[str] = set()
    for f in sorted(d.glob("decision_*.json"))[-limit_files:]:
        try:
            j = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        p = j.get("pick") or {}
        if p.get("kind") != "curiosity":
            continue
        k = _topic_key(p.get("keyword") or p.get("title") or "")
        if k:
            keys.add(k)
    return keys


def curiosity_rotate_stamp() -> float | None:
    try:
        j = json.loads(CURIOSITY_ROTATE_STAMP.read_text(encoding="utf-8-sig"))
        return float(j.get("ts_unix") or 0) or None
    except Exception:
        return None


def refresh_curiosity() -> dict:
    """池子吃空 → 抓下一轮（带冷却，失败只记录不抛）。"""
    last = curiosity_rotate_stamp()
    if last and (time.time() - last) < CURIOSITY_REFRESH_COOLDOWN_H * 3600:
        return {"ok": False, "reason": f"距上次换轮 {(time.time() - last) / 3600:.1f}h"
                                       f" < {CURIOSITY_REFRESH_COOLDOWN_H}h，先等"}
    try:
        r = subprocess.run([sys.executable, str(SELF / "curiosity.py")],
                           cwd=str(SELF.parent), capture_output=True, timeout=90)
        out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
        ok = r.returncode == 0
        # 成功失败都盖时间戳：失败也进冷却，免得网络断了每轮都去重试一遍
        CURIOSITY_ROTATE_STAMP.write_text(
            json.dumps({"ts_unix": time.time(), "ok": ok,
                        "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
                        "out": out[-400:]}, ensure_ascii=False, indent=2), encoding="utf-8")
        first = (out.strip().splitlines() or [""])[0][:120]
        return {"ok": ok, "reason": first}
    except Exception as e:
        return {"ok": False, "reason": f"刷新失败：{e}"}


def load_curiosity(path: Path = CURIOSITY_FILE, ttl_h: float = CURIOSITY_TTL_H) -> list:
    """读好奇候选池；过期 / 解析失败一律当空，绝不让坏文件拖垮选事。

    过期判定按条目算：抓热搜的条目看 generated_at，外部追加的（刷视频）看自己的 added_at，
    否则一条旧池子里的新词会被整体时间戳一起判死。
    """
    try:
        d = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []

    def _fresh(ts, t_h=None) -> bool:
        try:
            t = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            return (now() - t).total_seconds() <= float(t_h or ttl_h) * 3600
        except Exception:
            return False

    doc_fresh = _fresh(d.get("generated_at"))
    out = []
    for it in d.get("items") or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        # 有自己时间戳的（外部追加）只认自己的；热搜来的看池子时间
        if "added_at" in it:
            # 外部追加的常驻片单（装几%猿之类）自带 ttl_h，不跟着热点 48h 一起死
            try:
                _own_ttl = float(it.get("ttl_h") or ttl_h)
            except Exception:
                _own_ttl = ttl_h
            if not _fresh(it.get("added_at"), _own_ttl):
                continue
        elif not doc_fresh:
            continue
        g = dict(it)
        # 池子里不止好奇：灵感（kind=inspiration）与好奇同级，不许被压成 curiosity
        _k = it.get("kind")
        g["kind"] = _k if _k in ("curiosity", "inspiration") else "curiosity"
        g.setdefault("cost_rmb", 0)
        out.append(g)
    return out


def pick(goals: list, lv: str, gate_state: str, idle_h: float,
         streak: dict | None = None, no_explore: bool = False,
         quota: str = "unknown", wallet: float | None = None,
         history: dict | None = None, meta_ok: bool = True,
         optimize_ok: bool = True, curiosity_ok: bool = True,
         kind_streak_info: tuple | None = None,
         cat_streak_info: tuple | None = None) -> tuple[dict | None, str, bool]:
    """返回 (目标, 由头, 是否被余额卡住)。第三位给 collect_signals 分辨「穷」和「被拒」。"""
    ceiling = COST_CEILING[lv]
    if wallet is not None:
        ceiling = min(ceiling, SINGLE_ROUND_CAP_RMB, float(wallet))
    streak = streak or {}
    history = history or {}
    pool = []
    money_block = False
    for g in goals:
        if g.get("done") or g.get("status") == "done":
            continue
        if g.get("kind") == "explore":
            if gate_state in ("no_explore", "stop"):
                continue
            if no_explore:
                continue  # 今天已经开过一次新坑，够了
        if g.get("kind") == "meta":
            # 自我优化轮：冷却没过 / 身体疼 / 没写候选数 → 一律不进池
            if not meta_ok:
                continue
            if int(g.get("candidates_required", 0) or 0) < META_MIN_CANDIDATES:
                continue  # 没有候选要求的行动就是拍脑袋，不许进池
        if g.get("kind") == "optimize":
            if not optimize_ok:
                continue  # 优化轮冷却中：上一次优化没隔够 OPTIMIZE_COOLDOWN_H
        if g.get("kind") == "curiosity":
            if not curiosity_ok:
                continue  # 今天额度用完 / 状态不行 / 池子空
        raw_cost = float(g.get("cost_rmb", 0) or 0)
        gkind = str(g.get("kind") or "")
        # 成本预测不用同类均值（主人 11-13 否）：同 kind 里大改小改差十倍，均值是假动作。
        # 单轮成本由「这一步要读多少材料」决定，所以只认目标自己填的 cost_rmb。
        cost = raw_cost
        need = raw_cost
        if quota == "empty" and raw_cost > 0:
            continue  # 工资池见底，只许白嫖动作
        if need > ceiling:
            if wallet is not None and need > float(wallet):
                money_block = True  # 不是不能做，是还没攒够
            continue
        # 攒钱专用目标不按成本惩罚 —— 攒够了就是要干这件事，否则永远打不过零成本小活
        w_cost = 1.0 if g.get("need_savings") else 5.0
        score = float(g.get("priority", 5)) * 10 - cost * w_cost
        if g.get("kind") == "meta":
            score -= 8  # 改自己让位于止血动作，但价值层会把它拉回到验证之前
        score += VALUE_BONUS.get(g.get("kind"), 0)  # 价值序：学习 > 优化 > 验证 > 空转
        if g.get("kind") == "maintain":
            score -= MAINTAIN_PENALTY  # 省力空转不再优先；巡检只在别的事都做不了时上
        # 空转奖励已删：原「闲久了给 maintain +4」等于奖励空转（主人 11-05 指令）
        st = int(streak.get(g.get("id"), 0))
        if st:
            score -= 15 * st  # 连着被建议过就往后排，别原地转圈
        if kind_streak_info:
            lk, ln = kind_streak_info
            if lk and g.get("kind") == lk and ln >= 2:
                score -= 8 * min(3, ln - 1)  # 换了 id 还是同一类动作，一样算原地转圈
        if cat_streak_info:
            lc, ln = cat_streak_info
            # 分类垄断：连吃同一分类就压低，同时给别的分类冒泡，逼它换档（不禁止，只调序）
            # 注意条目上的字段名是 cat，decision 里记的是 curiosity_cat，两个都得认
            if lc and g.get("kind") == "curiosity" and ln >= 2:
                gcat = g.get("curiosity_cat") or g.get("cat")
                if gcat == lc:
                    score -= 10 * min(2, ln)
                else:
                    score += 10
        stale = int(history.get(g.get("id"), 0))
        if stale:
            score += min(STALE_CAP, STALE_STEP * stale)  # 久没被选过就冒泡上浮
        pool.append((score, g))
    if not pool:
        return None, (
            f"没有成本≤{round(ceiling,3)} 且合规的可做目标"
            f"（段位={lv}，闸门={gate_state}，池={quota}，余额={wallet}）"
        ), money_block
    pool.sort(key=lambda x: (-x[0], random.random()))  # 同分随机，避免固定顺序垄断
    if os.environ.get("IC_PICK_DEBUG"):
        try:
            (SELF / "iter_out" / "pick_debug.json").write_text(json.dumps([
                {"score": round(float(sc), 1), "kind": g.get("kind"),
                 "cat": g.get("curiosity_cat") or g.get("cat"),
                 "title": (g.get("title") or g.get("keyword") or "")[:44]}
                for sc, g in pool], ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    return pool[0][1], (
        "价值序排序（学习>优化>验证>空转）：priority×10 - cost，explore+12/curiosity+10/"
        "meta·optimize+8、maintain-10 且不再享受闲置加分；连续命中降权、久未选中冒泡，同分随机"
    ), False


DEFAULT_GOALS = {
    "_note": "自迭代的候选池。cost_rmb=0 表示不烧钱；kind: maintain=维护 / explore=探索 / archive=归档",
    "goals": [
        {"id": "g1", "title": "巡检自迭代信号链（collect_signals → bio_signals → compute_pain）是否还在跑", "kind": "maintain", "cost_rmb": 0, "priority": 3},
        {"id": "g2", "title": "把当天对话归档进 self/log.md 与 changes.md", "kind": "archive", "cost_rmb": 0, "priority": 2},
        {"id": "g3", "title": "检查 cron 与进程健康（6199/6185、定时任务是否还在）", "kind": "maintain", "cost_rmb": 0, "priority": 3},
        {"id": "g4", "title": "学一个可复用技能，写进 skills/", "kind": "explore", "cost_rmb": 0.2, "priority": 3},
        {"id": "g5", "title": "清 plugin_data 残留与旧备份", "kind": "maintain", "cost_rmb": 0, "priority": 1,
         "tier": "t4_irreversible", "needs": "口令 + 当场确认（删数据）"},
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="写文件并追加 log.md")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--round-start", default=None, metavar="LABEL",
                    help="一轮 agent 唤醒开始：记下 DSH 真实余额")
    ap.add_argument("--round-end", action="store_true",
                    help="一轮结束：余额差值扣进工资钱包")
    ap.add_argument("--no-charge", action="store_true",
                    help="配合 --round-end：只记账不扣钱（调试用）")
    ap.add_argument("--cost-report", action="store_true",
                    help="按动作类型看真实花费历史：均值/中位/P90/最大（只观察，不参与选事）")
    ap.add_argument("--goal-cost", action="store_true",
                    help="按目标/方向看真实花费：走了几轮、累计多少、单轮峰值")
    ap.add_argument("--cost-json", action="store_true",
                    help="配合 --cost-report：输出 JSON")
    args = ap.parse_args()

    if args.round_start is not None:
        print(json.dumps(round_start(args.round_start), ensure_ascii=False))
        return 0
    if args.round_end:
        print(json.dumps(round_end(charge=not args.no_charge), ensure_ascii=False))
        return 0
    if args.goal_cost:
        rows = goal_cost_stats()
        if not rows:
            print("还没积累到有效样本（round_costs.jsonl 空或全是 0）")
            return 0
        print(f"{'方向':<44}{'轮':>3}{'累计':>8}{'单轮均':>8}{'单轮峰':>8}  最近")
        for k, v in rows.items():
            title = (v["title"] or v["goal"])[:20]
            print(f"{title:<44}{v['rounds']:>3}{v['total']:>8}{v['mean']:>8}{v['max']:>8}  {v['last']}")
        print("\n注：均值只做观察。同一方向里大改和小改差十倍，选事不看这个数。")
        return 0
    if args.cost_report:
        st = cost_stats()
        if args.cost_json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return 0
        if not st:
            print("还没积累到有效样本（round_costs.jsonl 空或全是 0）")
            return 0
        print(f"{'类型':<12}{'样本':>4}{'均值':>8}{'中位':>8}{'P90':>8}{'最大':>8}")
        for k, v in st.items():
            print(f"{k:<12}{v['n']:>4}{v['mean']:>8}{v['median']:>8}{v['p90']:>8}{v['max']:>8}")
        est_lines = []
        print("\n注：同类型里大改/小改能差十倍，这些数只做观察，不参与选事。")
        return 0

    goals_file = SELF / "goals.json"
    if not goals_file.exists():
        if args.commit:
            goals_file.write_text(json.dumps(DEFAULT_GOALS, ensure_ascii=False, indent=2), encoding="utf-8")
        goals = DEFAULT_GOALS["goals"]
    else:
        goals = read_json(goals_file, DEFAULT_GOALS).get("goals", [])

    # 好奇候选只在内存里合并，不回写 goals.json（热点凉了自动消失）
    base_goals = list(goals)
    consumed = consumed_curiosity_ids()
    consumed_topics = consumed_curiosity_topics()
    all_cur = load_curiosity()

    def _fresh_cur(items: list, ids: set, topics: set) -> list:
        """挑没吃过的：候选 id 一次都不许重吃，同主题（跨天换 id）也不许重吃。"""
        return [g for g in items
                if str(g.get("id")) not in ids
                and _topic_key(g.get("keyword") or g.get("title")) not in topics]

    cur_items = _fresh_cur(all_cur, consumed, consumed_topics)
    # 全池吃完 → 顺位到头，起下一轮（带冷却，防同一天空转刷网络）
    rotate = None
    if all_cur and not cur_items:
        rotate = refresh_curiosity()
        consumed = consumed_curiosity_ids()
        consumed_topics = consumed_curiosity_topics()
        all_cur = load_curiosity()
        cur_items = _fresh_cur(all_cur, consumed, consumed_topics)
    goals = base_goals + cur_items

    pain, signals = compute_pain()
    pain_src = "self/health.json + bio_signals.json + 端口探针"
    if signals.get("stale"):
        pain_src += "（bio_signals 已过期，未计入）"
    lv = level_of(pain)
    last = last_log_ts(SELF / "log.md")
    idle_h = round((now() - last).total_seconds() / 3600, 2) if last else float("inf")
    balance = read_balance_rmb()
    g_state, g_ratio = gate(balance)

    wallet_bal, pay_days = payroll()
    decisions = load_recent_decisions()
    streak = pick_streak(decisions)
    history = stale_rounds(decisions, [g.get("id") for g in goals])
    quota_s, _, _ = quota_state()
    # 自我优化环节的门禁：冷却 + 身体状态 + 上一轮有没有白改
    meta_cd_ok, meta_since = meta_cooldown(decisions)
    meta_pending = meta_pending_unused()
    meta_ok = (meta_cd_ok and pain < META_MAX_PAIN and lv in ("normal", "throttle")
               and meta_pending is None)
    # 优化轮冷却：同一类「改已有程序」的动作 36h 内只许开一次
    opt_cd_ok, opt_since = optimize_cooldown(decisions)
    optimize_ok = (opt_cd_ok and lv in ("normal", "throttle")
                   and any(g.get("kind") == "optimize" for g in base_goals))
    # 好奇门禁：池里还有没吃过的 + 身体不疼（轮询式，吃一条顺位往下）
    curiosity_ok = (bool(cur_items) and pain < CURIOSITY_MAX_PAIN
                    and lv in ("normal", "throttle"))
    k_streak = kind_streak(decisions)
    c_streak = cat_streak(decisions)
    goal, why, money_block = pick(goals, lv, g_state, 0 if idle_h == float("inf") else idle_h,
                                  streak=streak, no_explore=explored_today(decisions),
                                  quota=quota_s, wallet=wallet_bal, history=history,
                                  meta_ok=meta_ok, optimize_ok=optimize_ok,
                                  curiosity_ok=curiosity_ok, kind_streak_info=k_streak,
                                  cat_streak_info=c_streak)
    blocked = None
    if lv == "halt":
        goal, why, blocked = None, "pain≥85 停机待审，本轮只记录不执行", "halt"
    elif g_state == "stop":
        goal, why, blocked = None, f"余额{balance}rmb < 闸门{GATE_STOP_RMB}rmb，自主全停，只记录汇报", "gate"
    elif quota_s == "empty" and goal is None:
        goal, why, blocked = None, f"工资池见底（{wallet_bal}rmb），且没有零成本目标可做", "quota"
    elif goal is None and money_block:
        blocked = "money"

    decision = {
        "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
        "pain": pain,
        "level": lv,
        "pain_source": pain_src,
        "idle_hours": None if idle_h == float("inf") else idle_h,
        "balance_rmb": balance,
        "gate": g_state,
        "pick": goal,
        "why": why,
        "tier": (goal or {}).get("tier", "t2_write_low"),
        "cost_rmb": float((goal or {}).get("cost_rmb", 0) or 0),
        "quota_state": quota_s,
        "wallet_rmb": wallet_bal,
        "payroll_rmb": PAYROLL_RMB,
        "payroll_days": pay_days,
        "blocked": blocked,
        "pick_streak": streak,
        "kind_streak": {"kind": k_streak[0], "n": k_streak[1]},
        "cat_streak": {"cat": c_streak[0], "n": c_streak[1]},
        "pick_history": history,
        "explored_today": explored_today(decisions),
        "meta": {
            "ok": meta_ok,
            "cooldown_h": META_COOLDOWN_H,
            "since_h": meta_since,
            "pending_unused": (meta_pending or {}).get("ts"),
            "min_candidates": META_MIN_CANDIDATES,
            "max_pain": META_MAX_PAIN,
            "forbidden": list(META_FORBIDDEN),
        },
        "optimize": {
            "ok": optimize_ok,
            "cooldown_h": OPTIMIZE_COOLDOWN_H,
            "since_h": opt_since,
        },
        "curiosity": {
            "ok": curiosity_ok,
            "pool": len(cur_items),
            "pool_total": len(all_cur),
            "consumed": len(consumed),
            "rotate": rotate,
            "ttl_h": CURIOSITY_TTL_H,
            "cats": sorted({it.get("curiosity_cat", "") for it in cur_items} - {""}),
        },
    }

    if args.json:
        print(json.dumps(decision, ensure_ascii=False, indent=2))
    else:
        print(f"[{decision['ts']}] pain={pain} 段位={lv} 闲置={decision['idle_hours']}h "
              f"账本余额={balance}rmb 闸门={g_state}")
        print(f"  信号来源: {pain_src}")
        print(f"  本轮建议: {goal['title'] if goal else '（不动）'}")
        print(f"  由头: {why}")
        print(f"  工资池: {wallet_bal}rmb（{quota_s}，本次发薪{pay_days}天） streak={streak or '无'}")
        if meta_ok:
            print(f"  自我优化轮: 可开（距上轮 {meta_since or '从未'}h，需≥{META_MIN_CANDIDATES} 个候选变体）")
        elif meta_pending:
            print(f"  自我优化轮: 被上一轮白改挡住（{meta_pending.get('ts')} 那条没有 used_by，先补用）")
        else:
            print(f"  自我优化轮: 关（距上轮 {meta_since}h/{META_COOLDOWN_H}h，pain={pain}/{META_MAX_PAIN}）")
        if optimize_ok:
            print(f"  优化轮: 可开（距上次 {opt_since or '从未'}h，冷却 {OPTIMIZE_COOLDOWN_H}h）")
        else:
            print(f"  优化轮: 关（距上次 {opt_since}h/{OPTIMIZE_COOLDOWN_H}h，或状态不允许）")
        print(f"  好奇池: 待吃 {len(cur_items)}/{len(all_cur)} 条"
              f"（{','.join(decision['curiosity']['cats']) or '空'}）"
              f" 已吃 {len(consumed)} → {'可开' if curiosity_ok else '关'}")
        if rotate:
            print(f"  换轮: {'成功' if rotate.get('ok') else '跳过'}｜{rotate.get('reason', '')}")
        if goal:
            print(f"  定级: {decision['tier']}  成本: {decision['cost_rmb']} rmb")

    if args.commit:
        out_dir = SELF / "iter_out"
        out_dir.mkdir(exist_ok=True)
        write_health(pain, signals)
        if goal and decision["cost_rmb"] > 0:
            if not wallet_spend(decision["cost_rmb"], goal.get("title", "")):
                goal = None
                decision.update({"pick": None, "cost_rmb": 0.0, "blocked": "money",
                                 "why": "判定后余额被花掉，本轮作废"})
        if goal:
            add_self_cost(decision["cost_rmb"])
        # 带毫秒 + 撞名兜底：同一秒连跑不再互相覆盖（2026-11-04 主人口令）
        stem = now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        dpath = out_dir / f"decision_{stem}.json"
        n = 1
        while dpath.exists():
            dpath = out_dir / f"decision_{stem}_{n}.json"
            n += 1
        dpath.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
        line = (
            f"{decision['ts']} | 建议: {goal['title'] if goal else '（停滞）'} | "
            f"pain={pain}/{lv} idle={decision['idle_hours']}h gate={g_state} bal={balance}rmb | "
            f"wallet={wallet_bal}rmb({quota_s}) streak={streak or '-'} | "
            f"cur={len(cur_items)}/{len(all_cur)} eaten={len(consumed)}"
            + (f" rotated={'ok' if (rotate or {}).get('ok') else 'skip'}" if rotate else "")
            + f" | {decision['cost_rmb']} rmb | pending\n"
        )
        log = SELF / "log.md"
        log.write_text(log.read_text(encoding="utf-8") + line, encoding="utf-8")

        # ---- 自带「总结 + 瘦身」（2026-11-12 主人派活）----
        # 每轮刷一次总结（O(文件数)，很轻）；只有 meta/optimize 这类优化轮才真瘦身，
        # 免得每半小时动一次文件系统。出错只记录，不影响本轮结论。
        hk = {}
        try:
            import housekeep
            hk["summary"] = housekeep.summarize(True)
            if (goal or {}).get("kind") in ("meta", "optimize"):
                r = housekeep.slim(True)
                hk["slim"] = {"freed_mb": r["freed_mb"], "actions": len(r["log"])}
                for l in r["log"]:
                    log.write_text(log.read_text(encoding="utf-8") + f"    housekeep: {l.strip()}\n",
                                   encoding="utf-8")
        except Exception as e:
            hk["error"] = f"{type(e).__name__}: {e}"
        if hk:
            decision["housekeep"] = hk
            try:
                dpath.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            extra = (f" | housekeep: {hk.get('slim', {}).get('freed_mb', 0)}MB"
                     if hk.get("slim") else "")
            if hk.get("error"):
                extra += f" | housekeep 出错 {hk['error']}"
            if extra:
                log.write_text(log.read_text(encoding="utf-8").rstrip("\n") + extra + "\n",
                               encoding="utf-8")

        # ---- 每轮评分（2026-11-13 主人定：整个自迭代每轮都参与，不单开优化轮）----
        # 机械判官零成本，跟着 commit 就跑；子代理只出任务包，花不花钱由主循环决定。
        # 任何异常只记录，绝不打断本轮结论。
        try:
            if str(SELF) not in sys.path:
                sys.path.insert(0, str(SELF))
            import score_judge as _sj
            res = _sj.score_decision(decision)
            decision["score"] = res
            _sj.write_history("decision", res)
            arc = _sj.score_archive(commit=True)
            if arc:
                decision["score_archive"] = [
                    {"ts": r.get("target_ts"), "score": r.get("score"), "flags": r.get("flags")}
                    for r in arc
                ]
            dpath.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
            _pstat = (res.get("panel") or {}).get("status", "done")
            log.write_text(
                log.read_text(encoding="utf-8")
                + "    score: 决策={:.1f} 面板={} flags={}{}\n".format(
                    res.get("score", 0), _pstat,
                    ",".join(res.get("flags") or []) or "-",
                    f" archive+{len(arc)}" if arc else "")
                , encoding="utf-8")
        except Exception as e:
            decision["score_error"] = f"{type(e).__name__}: {e}"
    return 0


if __name__ == "__main__":
    sys.exit(main())
