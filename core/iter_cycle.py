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

HERE = Path(__file__).resolve().parent  # 代码目录
SELF = Path(os.environ.get("SELFDRIVE_HOME") or HERE)  # 数据目录（可外置）
BASE = SELF.parent
CST = timezone(timedelta(hours=8))

try:  # 开关中心：链路总开关 + 功能子开关（self/switchboard.py + switches.json）
    import switchboard as _swb
except Exception:  # 文件丢了也不许把功能静默关掉
    _swb = None


def _on(path: str) -> bool:
    """查开关。模块缺失/异常一律按开 —— 开关坏了顶多没关掉，不能变成全瘫。"""
    if _swb is None:
        return True
    try:
        return _swb.on(path)
    except Exception:
        return True


def _chains() -> dict:
    """给 decision 落盘用：每条链的开与关闭子项。"""
    if _swb is None:
        return {"_error": "switchboard 未加载"}
    try:
        return _swb.summary()
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}

LEVELS = ("normal", "throttle", "readonly", "halt")
# 段位只约束「一轮能烧多少」，不再兼任日配额；真上限由钱包余额和 SINGLE_ROUND_CAP_RMB 卡
COST_CEILING = {"normal": 5.0, "throttle": 2.0, "readonly": 0.0, "halt": 0.0}
# 决策候选是否落 jev/rounds.jsonl。--show 只读时必须关：否则每看一眼就多一行，
# convergence 的「同 id 24h 重复」会把自己观测到的轮次算成反复中选（2026-11-17 修）。
PICK_LOG_ENABLED = True


def _picklog_off() -> bool:
    """只读观测（--show 及其子进程）不许往 rounds.jsonl 追加。

    原来只靠内存里的 PICK_LOG_ENABLED 关，但 --show 查池子会 spawn 子进程，
    子进程拿到的是默认 True，于是每看一次照样追加 5 行（2026-11-17 抓：
    跑一次 --show，行数 1293→1298；同秒多行就是这么攒出来的）。
    环境变量被子进程继承，所以真正的闸放这里。
    """
    return os.environ.get("IC_PICK_LOG", "1").strip().lower() in ("0", "false", "no", "off")
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
# 价值序不再按「听起来有用」排，按 rounds.jsonl 里人工标注的实测平均 y 排（2026-11-17 校准）。
# 证据见 self/jev/rounds.jsonl：archive 1.00(n=6) / maintain 1.00(n=3) / inspiration 0.83(n=6)
# / introspect 0.75(n=4) / curiosity 0.43(n=14) / explore 0.00(n=12)。
# 结论很反直觉：判据明确的轻活（归档、巡检）真被验收；「学个可复用技能」这类开新坑 12 次零落地。
# 回归测试 self/test_jev_kind.py 断言 bonus 排序与实测 y 的 Kendall tau ≥ 0.6，改这里必须重跑它。
VALUE_BONUS = {"archive": 10, "maintain": 9, "inspiration": 9, "introspect": 8,
               "curiosity": 5, "explore": 4, "meta": 4, "optimize": 4}
# 2026-11-18 起 VALUE_BONUS 只当「一个样本都没有」时的兜底：有样本就走
# jev_ledger.bonus_for_pool 的收缩公式（向 4 分靠，人工标注权重 3 倍）。


def value_bonus(kind, prior: dict | None = None) -> float:
    """价值加分：台账实测优先，样本不足退先验。台账坏了也退先验，不许因此 pick 崩。"""
    pm = prior or VALUE_BONUS
    base = float(pm.get(kind, 0))
    try:
        from jev_ledger import bonus_for_kind
        return float(bonus_for_kind(kind, pm))
    except Exception:
        return base
# explore 11-17 从 1 提到 4：主人当天把探索轮重定义成「学习轮」——按大学课程表补缺技能、
# 同方向合并（self/learn.py），不再是「随便学个技能开新坑」。1 分是旧口径 12 次零落地的实测，
# 提到 4 后仍排最低档；若接下来 3 轮 explore 依然没有可验收产出，按 1 分回退。
# 排序单调性靠 self/test_jev_kind.py 的 Kendall tau ≥0.6 守着，改这里必须重跑它。
# maintain 以前是 0 分再 -10，实测 y 却是满分：那是拿惩罚替代了验收。
# 现在只保留「别让它独占」的量，-4 后有效分 5，与 curiosity 同档。
MAINTAIN_PENALTY = 4.0

# ---- 方向脑（主人 2026-11-17：进化优先、其次赚钱）----
# direction.py 算出的方向分（约 -6..+7）乘这个系数后并入打分。
# 取 2.0 是为了和 streak 的 -15 同量级：能明显调序，但压不死成本与收敛护栏。
DIRECTION_W = 1.5


# ---- 六池轮转（主人 2026-11-17）：一轮里每池只跑一次，跑过的池本轮不再进候选 ----
POOL_GROUPS = {
    "curiosity": ("curiosity",),
    "optimize": ("meta", "optimize"),
    "inspiration": ("inspiration",),
    "learn": ("explore",),          # 学习轮的 kind 就叫 explore（learn.py 落盘口径），池名用「学习」
    "introspect": ("introspect",),  # 自省独立成池（2026-11-17 主人：八池）
    "practice": ("practice",),
    "maintain": ("maintain",),
    "archive": ("archive",),
}
POOL_ORDER = ["curiosity", "learn", "inspiration", "introspect", "optimize", "practice", "maintain", "archive"]
ROTATION_FILE = SELF / "pool_rotation.json"


def pool_of(kind) -> str | None:
    """kind → 所属池名。meta 和 optimize 算同一个「优化」池。"""
    k = str(kind or "")
    for p, kinds in POOL_GROUPS.items():
        if k in kinds:
            return p
    return None


def load_rotation() -> dict:
    d = read_json(ROTATION_FILE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("round", 1)
    if not d.get("started"):
        d["started"] = now().strftime("%Y-%m-%d %H:%M:%S")
    # used 里可能存过 kind 名（旧口径 "explore"），统一归一到池名，否则按池比对会错位成"还没跑"
    d["used"] = [p for p in (pool_of(u) or u for u in (d.get("used") or [])) if p in POOL_GROUPS]
    if not isinstance(d.get("history"), list):
        d["history"] = []
    return d


def save_rotation(d: dict) -> None:
    try:
        clean = {k: v for k, v in d.items() if not k.startswith("_")}
        ROTATION_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


MIN_POOLS_PER_ROUND = 1   # 一轮跑过 1 个池就算跑完（2026-11-17 20:50 改平级轮询：一轮只轮到一个池）
CLOSE_IDEMPOTENT_S = 60   # 同一轮的关闭记录 60 秒内不重复记


def _recent_close(rot: dict, seconds: int = CLOSE_IDEMPOTENT_S) -> bool:
    """上一轮刚关过就别再记一条：auto_close 被连续调用时会两秒关两轮。"""
    h = rot.get("history") or []
    if not h:
        return False
    try:
        t = datetime.strptime(str(h[-1].get("closed")), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    n = now()
    if getattr(n, "tzinfo", None) is not None:   # now() 带时区，strptime 出来的不带
        n = n.replace(tzinfo=None)
    return (n - t).total_seconds() < seconds


def rotation_window(goals: list) -> tuple[set, dict, str]:
    """算出本轮还能跑哪些池。返回 (允许的池集合, 轮次状态, 说明)。

    规则：新的一轮先跑好奇；之后每轮只在不重复的池里挑；可用的池全跑过就开新轮。
    只按「池里此刻有没有目标」算可用——被门禁挡死的池不会把轮次卡住。
    """
    rot = load_rotation()
    avail = {p for p in (pool_of(g.get("kind")) for g in goals) if p}
    used = set(rot["used"])
    extra = f"，池里没货：{'/'.join(p for p in POOL_ORDER if p not in avail) or '无'}"
    if not avail:
        return set(), rot, "没有任何池有目标"
    if not used or not (avail - used):
        if used and len(used) >= MIN_POOLS_PER_ROUND:
            if _recent_close(rot):
                # 60 秒内已经关过这轮：盘上 used 是脏的（早退路径没走到调用方那行 save_rotation），
                # 清掉就完事，别再记一条 history 把 round 数字吹上去。
                rot["used"] = []
                used = set()
                save_rotation(rot)
            else:
                rot["history"].append({"round": rot["round"], "used": sorted(used),
                                       "closed": now().strftime("%Y-%m-%d %H:%M:%S")})
                rot["round"] += 1
                rot["used"] = []
                used = set()
                rot["started"] = now().strftime("%Y-%m-%d %H:%M:%S")
                save_rotation(rot)   # 立刻落盘：不然早退时这轮关闭根本没保存，下一脚又关一次
        elif used and len(used) < MIN_POOLS_PER_ROUND:
            # 有货的池不够一轮的池数：不关轮计数，把剩下的全放出来接着跑。
            # 否则 archive/explore/maintain 三个池跑完就两秒关一轮，
            # curiosity 和 inspiration 永远排不上（今天 round 3→4 就是这么来的）。
            keep = avail
            rot.setdefault("started", now().strftime("%Y-%m-%d %H:%M:%S"))
            return keep, rot, (f"第{rot['round']}轮，池里只有 {len(avail)} 个够跑"
                               f"（<{MIN_POOLS_PER_ROUND}），不关轮，全放出来："
                               f"{'/'.join(p for p in POOL_ORDER if p in keep)}" + extra)
        start = "curiosity" if "curiosity" in avail else next((p for p in POOL_ORDER if p in avail), None)
        keep = {start} if start else set()
        rot.setdefault("started", now().strftime("%Y-%m-%d %H:%M:%S"))
        why = f"第{rot['round']}轮开轮，先跑 {start}"
    else:
        # 平级轮询（2026-11-17 主人要求）：按 POOL_ORDER 固定顺序，一次只轮到一个池，
        # 不由打分插队——打分只在池内挑条目，池之间是等权的。
        nxt = next((p for p in POOL_ORDER if p in avail and p not in used), None)
        keep = {nxt} if nxt else set()
        why = (f"第{rot['round']}轮，已跑 {'/'.join(sorted(used)) or '无'}，"
               f"轮到 {'/'.join(p for p in POOL_ORDER if p in keep) or '无'}")
    return keep, rot, why + extra


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
        if _on("signal.collect") and fresh and isinstance(ext.get(k), (int, float)):
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
        if _on("signal.stale_block") and isinstance(h.get("pain"), (int, float)):
            # 闭环：信号过期就不许拿旧数据把痛感打折，按上一轮记的值垫底
            pain = max(pain, float(h["pain"]))
            meta["stale_blocked"] = round(float(h["pain"]), 1)
    if not _on("signal.pain"):
        meta["pain_off"] = round(pain, 1)
        pain = 0.0
    return round(min(100.0, max(0.0, pain)), 1), meta


def write_health(pain: float, signals: dict):
    p = SELF / "health.json"
    h = read_json(p, {}) or {}
    h["pain"] = pain
    h["signals"] = {k: {"value": v} for k, v in signals.items() if k != "stale"}
    # ts 以前只写 updated_at，ts/ts_unix 冻在 11-14，任何按 ts 判新的读法都会误判
    ts = now()
    h["ts"] = ts.strftime("%Y-%m-%d %H:%M:%S")
    h["ts_unix"] = ts.timestamp()
    h["updated_at"] = h["ts"]
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
    """余额接入是私货位（见 local/README.md）。

    自己实现一个 ledger.py（放 SELFDRIVE_HOME 或 core/），提供
    snapshot() -> {"balance": float, "cost_today": float}。没实现就抛，调用方按"查不到"处理。
    """
    if str(SELF) not in sys.path:
        sys.path.insert(0, str(SELF))
    try:
        import ledger as _l  # 自己实现的余额适配器
    except ImportError as e:
        raise ImportError("缺 ledger 适配器：在 SELFDRIVE_HOME 下实现 snapshot()") from e
    return _l


def read_budget() -> dict:
    """已退役（2026-11-02 18:20 起改工资池 wallet.json）。留着只为读旧字段做展示。"""
    """今日自配额（主人给的 1 rmb 怎么切），没有就返回空。"""
    return read_json(SELF / "budget.json", {}) or {}


def read_balance_rmb() -> float | None:
    """读余额：先环境变量 BIO_BALANCE_RMB，再问适配器，最后读快照文件。

    余额一律走本位币（人民币），不做汇率折算。
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
ORPHANS = SELF / "round_mark_orphans.jsonl"   # 没结账就被覆盖的轮次标记（2026-11-17 加）


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


def elapsed_stats(max_round_s: float = 90.0) -> dict:
    """单轮封包时长统计（2026-11-17 主人要求按时序定）。

    口径：settled_at − ts，取整秒。新轮直接用落盘的 elapsed_s，老轮用两个时间戳反算。
    空轮（<5 秒）不计：那是「开轮→发现没活干→结账」，不构成一轮。
    > max_round_s 记一次超时轮，用来判断该不该拆。
    """
    vals, over = [], []
    try:
        lines = ROUND_HISTORY.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        el = r.get("elapsed_s")
        if not isinstance(el, (int, float)):
            try:
                from datetime import datetime as _dt
                el = (_dt.strptime(str(r.get("settled_at")), "%Y-%m-%d %H:%M:%S")
                      - _dt.strptime(str(r.get("ts")), "%Y-%m-%d %H:%M:%S")).total_seconds()
            except Exception:
                continue
        el = float(el)
        if el < 5:
            continue
        vals.append(el)
        if el > max_round_s:
            over.append({"ts": r.get("ts"), "kind": r.get("kind"), "elapsed_s": el})
    if not vals:
        return {"n": 0}
    v = sorted(vals)
    n = len(v)
    return {"n": n,
            "median": round(v[n // 2], 1),
            "p75": round(v[min(n - 1, int(n * 0.75))], 1),
            "p90": round(v[min(n - 1, int(n * 0.9))], 1),
            "max": round(v[-1], 1),
            "over_limit": len(over), "limit_s": max_round_s, "worst": over[-3:]}


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
    """一轮 agent 唤醒的开头：把基线刷新成「此刻的余额 + 今日消耗」。

    iter_cycle.py 本身是纯标准库脚本，跑它不烧 token；烧钱的是被唤醒后
    读文件、思考的我。所以这个标记要卡在整轮两端，不是脚本执行前后。
    基线每轮重取，不复用上一轮残留：余额走官方接口实时拉，今日消耗一并记下，
    结账时优先用「当日消耗增量」算这一轮，跨天/换源才退回余额差。
    """
    if not _on("exec.round_start"):
        return {"ok": False, "reason": "exec.round_start 开关关，不记基线"}
    snap = {}
    try:
        snap = _ledger().snapshot() or {}
    except Exception:
        snap = {}
    bal = snap.get("balance")
    if bal is None:
        bal = read_balance_rmb()
    spent_today = snap.get("today_spent")
    # 上一轮的标记还没结账就要被覆盖：先留一份，别让那轮的基线静默消失。
    # （2026-11-17 15:05 那轮 settled:false 就是这么被下一次 --round-start 吞掉的）
    old = read_json(ROUND_MARK, {}) or {}
    orphaned_prev = None
    if old and not old.get("settled") and not old.get("orphaned"):
        try:
            with ORPHANS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"orphaned_at": now().strftime("%Y-%m-%d %H:%M:%S"),
                                     "reason": "上一轮没跑 --round-end 就被新的 --round-start 覆盖",
                                     **old, "orphaned": True}, ensure_ascii=False) + "\n")
            orphaned_prev = old.get("ts")
        except Exception as e:
            orphaned_prev = f"归档失败：{str(e)[:60]}"
    mark = {
        "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
        "day": now().strftime("%Y-%m-%d"),
        "balance": bal,
        "today_spent": spent_today if isinstance(spent_today, (int, float)) else None,
        "source": snap.get("source"),
        "label": str(label or "")[:40],
        "settled": False,
        "orphaned": False,
    }
    ROUND_MARK.write_text(json.dumps(mark, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, **mark, "wallet": wallet_state()[1], "orphaned_prev": orphaned_prev}


def round_end(charge: bool = True) -> dict:
    """一轮结束：结账这一轮的花费，从工资钱包扣。

    基线在 --round-start 时刷新（余额 + 当日消耗），这里再取一次：
    同日且两端都有今日消耗，就按「当日消耗增量」算——这样充值、换源、
    别的时间段的余额跳变都不会算进来；跨天或拿不到消耗才退回余额差。
    两种口径都含同期别人的消耗（我自己回话、别的 cron），是上界不是精确值；
    负差值按 0 算，不当退款；钱包不够只扣到 0，不透支。
    """
    mark = read_json(ROUND_MARK, {}) or {}
    if not _on("exec.round_end"):
        return {"ok": False, "reason": "exec.round_end 开关关，本轮不结账"}
    if not mark:
        return {"ok": False, "reason": "没有轮次标记，先跑 --round-start"}
    if mark.get("settled"):
        return {"ok": False, "reason": "这一轮已经结过账", "settled_at": mark.get("settled_at")}
    start_bal = mark.get("balance")
    end_snap = {}
    try:
        end_snap = _ledger().snapshot() or {}
    except Exception:
        end_snap = {}
    end_bal = end_snap.get("balance")
    if end_bal is None:
        end_bal = read_balance_rmb()
    if start_bal is None or end_bal is None:
        return {"ok": False, "reason": "账本余额读不到（官方接口没应答），本轮不结账"}
    bal_delta = round(float(start_bal) - float(end_bal), 4)
    start_spent = mark.get("today_spent")
    end_spent = end_snap.get("today_spent")
    same_day = str(mark.get("day") or "") == now().strftime("%Y-%m-%d")
    if same_day and isinstance(start_spent, (int, float)) and isinstance(end_spent, (int, float)):
        diff = round(float(end_spent) - float(start_spent), 4)
        method = "today_delta"
    else:
        diff = bal_delta
        method = "balance_delta"
    if diff < 0:
        diff = 0.0
    before = wallet_state()[1]
    paid = 0.0
    if diff > 0 and charge:
        paid = min(diff, max(0.0, before))
        if paid > 0:
            wallet_spend(paid, f"真实token·{mark.get('label') or '轮次'}")
    pick = _latest_pick()
    # 单轮封包时长（2026-11-17 15:40 主人要求按时序定）：从 --round-start 到 --round-end
    # 的真实秒数。之前只有花费没有时长，定不出「一轮该包多久」。
    try:
        from datetime import datetime as _dt
        _el = round((_dt.now() - _dt.strptime(str(mark.get("ts")), "%Y-%m-%d %H:%M:%S")).total_seconds(), 1)
        if _el < 0:
            _el = None
    except Exception:
        _el = None
    mark.update({
        "elapsed_s": _el,
        "settled": True,
        "settled_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        "end_balance": end_bal,
        "spent": diff,
        "spent_method": method,
        "balance_delta": bal_delta,
        "start_spent": start_spent,
        "end_spent": end_spent,
        "charged": bool(diff > 0 and charge),
        "deducted": round(paid, 4),
        "wallet_before": before,
        "wallet_after": wallet_state()[1],
        "short": round(diff - paid, 4) if diff > paid else 0.0,
        "kind": str(pick.get("kind") or ""),
        "goal_id": str(pick.get("id") or ""),
    })
    # 维护轮（2026-11-17 主人定）：库 vs 现盘吻合分自评进 jev 台账，
    # 保证维护动作能被抓进标注闭环；台账挂了只记 error，不许影响结账。
    if str(pick.get("kind")) == "maintain":
        try:
            import maintain_audit as _ma
            mark["maintain_audit"] = _ma.record_round(pick) or {"skip": "台账没写进"}
        except Exception as e:
            mark["maintain_audit"] = {"error": f"{type(e).__name__}: {e}"[:80]}
    ROUND_MARK.write_text(json.dumps(mark, ensure_ascii=False, indent=2), encoding="utf-8")
    # Jev 回执：每轮结账就把已落盘的决策对回候选行，所有 kind 一视同仁。
    # 原来只有 08:00 日结跑一次，白天几轮全悬着 outcome=None，卡片发不出来。
    # 放在写 mark 之后、返回之前：回执失败也不许影响结账本身。
    try:
        from jev_calib import reconcile as _jev_reconcile
        mark["jev_reconcile"] = _jev_reconcile(quiet=True)
    except Exception as e:
        mark["jev_reconcile"] = {"error": str(e)[:80]}
    # 历史流水：一行一轮，按方向累计（这个方向一共烧了多少、走了几轮）

    try:
        with ROUND_HISTORY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": mark.get("ts"),
                "settled_at": mark["settled_at"],
                "elapsed_s": mark.get("elapsed_s"),
                "label": mark.get("label"),
                "kind": mark.get("kind") or "unknown",
                "goal_id": mark.get("goal_id") or "",
                "goal_title": str(pick.get("title") or "")[:60],
                "spent": diff,
                "spent_method": method,
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
CURIOSITY_REFRESH_COOLDOWN_H = 0.5  # 池子吃空后最少间隔 30min 再抓一轮（原来 3h，吃空就一整天空转）
CURIOSITY_REFRESH_DAILY_MAX = 8     # 一天最多抓 8 轮，防网络抖动时空转刷热搜
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


INTROSPECT_SCAN_FILES = 400


def consumed_introspect_ids(limit_files: int = INTROSPECT_SCAN_FILES) -> set:
    """扫历史决策，收集已经派过的自省条目（ins_ 开头）。

    自省条目的 id 是「源 + 原句」的稳定哈希，所以同一件烂尾事吃过一次就不再重复推。
    """
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
        if p.get("kind") == "introspect" and p.get("id"):
            ids.add(str(p["id"]))
    return ids


def load_introspect_items() -> list:
    """自省池：从 DANGLING/FACTS/alerts/changes 里推出来的活（不联网）。

    拿不到就返回空表 —— 自省是加分项，绝不允许它把整轮搞挂。
    """
    try:
        import introspect as _ins
        items = _ins.load()
    except Exception:
        return []
    eaten = consumed_introspect_ids()
    return [g for g in (items or []) if str(g.get("id")) not in eaten]


def consumed_optimize_ids(limit_files: int = INTROSPECT_SCAN_FILES) -> set:
    """已经派过的优化条目（opt_/meta_ 开头，id 是源+标题的稳定哈希）。"""
    d = SELF / "iter_out"
    if not d.exists():
        return set()
    ids: set[str] = set()
    for f in sorted(d.glob("*.json"))[-limit_files:]:
        try:
            j = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        p = j.get("pick") or {}
        gid = str(p.get("id") or "")
        if p.get("kind") in ("optimize", "meta") and gid:
            ids.add(gid)
    return ids


def load_optimize_items() -> list:
    """优化池：加法（补最小单测 / 拆超长函数）与减法（去重 / 删无人调用）。

    拿不到就返回空表 —— 优化是加分项，绝不把整轮搞挂。
    """
    try:
        import optimize as _opt
        items = _opt.load()
    except Exception:
        return []
    eaten = consumed_optimize_ids()
    return [g for g in (items or []) if str(g.get("id")) not in eaten]


def load_maintain_items() -> list:
    """维护池（2026-11-17 主人重定）：数据库=应然基线，现盘=实然。

    候选由 maintain_audit 比对生成（missing 一条一个、漂移/新脚本合并一条），
    吻合分写候选 score_hint，结账时进 jev 台账。审计挂了返回空表，
    goals.json 里的手写 maintain 条目继续兜底 —— 维护池不许把整轮搞挂。
    """
    try:
        import maintain_audit
        return maintain_audit.items() or []
    except Exception:
        return []


# 2026-11-17 主人：料源每次运行至少补一条；补得越少说明在空转，所以补了几条要记账。
POOL_MIN_PER_RUN = 1


def load_archive_items() -> list:
    """归档池料源（2026-11-17，第八池）：当天「上次归档之后」的新轮次压成一条增量归档。

    零成本、不联网；没有新轮次就返回空 —— 空就是空转证据，不硬造固定文案。
    """
    try:
        import archive_pool as _arc
        return _arc.items() or []
    except Exception:
        return []


def _pool_count(goals: list, pool: str) -> int:
    return sum(1 for g in (goals or []) if pool_of(g.get("kind")) == pool)


def refill_pools(goals: list) -> tuple[list, dict]:
    """缺料的池现场补一条（每池本次运行最多补 1 条），返回 (新候选, 记账)。

    各生成器按真实料源决定有没有货：池里本来还满着就不补，补 0 条如实记下来——
    那就是「池子没在消耗」的空转证据，不许造一条凑数。
    """
    have = {str(g.get("id")) for g in (goals or [])}
    added: list = []
    notes: list[str] = []

    def _take(items, pool: str, how: str) -> None:
        new = [g for g in (items or [])
               if pool_of(g.get("kind")) == pool and str(g.get("id")) not in have]
        if new:
            one = new[:POOL_MIN_PER_RUN]
            added.extend(one)
            have.update(str(g.get("id")) for g in one)
            notes.append(f"{pool}+{len(one)}({how})")
        else:
            notes.append(f"{pool}+0({how})")

    if _pool_count(goals, "curiosity") < POOL_MIN_PER_RUN and _on("pool.curiosity"):
        try:
            refresh_curiosity(force=True)
            _eaten = consumed_curiosity_ids()
            _take([g for g in load_curiosity() if str(g.get("id")) not in _eaten],
                  "curiosity", "curiosity.refresh")
        except Exception as e:
            notes.append(f"curiosity!{type(e).__name__}")
    if _pool_count(goals, "learn") < POOL_MIN_PER_RUN and _on("pool.learn"):
        try:
            import learn as _ln
            _ln.make()
            _take(load_curiosity(), "learn", "learn.make")
        except Exception as e:
            notes.append(f"learn!{type(e).__name__}")
    if _pool_count(goals, "inspiration") < POOL_MIN_PER_RUN and _on("pool.inspiration"):
        try:
            import inspiration as _insp
            _insp.make()
            _take(load_curiosity(), "inspiration", "inspiration.make")
        except Exception as e:
            notes.append(f"inspiration!{type(e).__name__}")
    if _pool_count(goals, "introspect") < POOL_MIN_PER_RUN and _on("pool.introspect"):
        try:
            import introspect as _ins
            _ins.build(write=True)
            _take(load_introspect_items(), "introspect", "introspect.build")
        except Exception as e:
            notes.append(f"introspect!{type(e).__name__}")
    if _pool_count(goals, "optimize") < POOL_MIN_PER_RUN and _on("pool.optimize"):
        try:
            import optimize as _opt
            _opt.build(write=True)
            _take(load_optimize_items(), "optimize", "optimize.build")
        except Exception as e:
            notes.append(f"optimize!{type(e).__name__}")
    if _pool_count(goals, "practice") < POOL_MIN_PER_RUN and _on("pool.practice"):
        try:
            import practice_pool as _pp
            _pp.fill()
            _take(load_practice_items(), "practice", "practice.fill")
        except Exception as e:
            notes.append(f"practice!{type(e).__name__}")
    if _pool_count(goals, "maintain") < POOL_MIN_PER_RUN and _on("pool.maintain"):
        try:
            _take(load_maintain_items(), "maintain", "maintain_audit")
        except Exception as e:
            notes.append(f"maintain!{type(e).__name__}")
    if _pool_count(goals, "archive") < POOL_MIN_PER_RUN and _on("pool.archive"):
        try:
            _take(load_archive_items(), "archive", "archive_pool")
        except Exception as e:
            notes.append(f"archive!{type(e).__name__}")

    rep = {
        "min": POOL_MIN_PER_RUN,
        "added": len(added),
        "notes": notes,
        "idle": not added,
        "counts": {p: _pool_count(goals + added, p) for p in POOL_ORDER},
    }
    return (goals + added), rep


def _round_started_ts() -> float:
    """本轮开轮时间（pool_rotation.json 的 started）；解析不了就返回 0（等于不做轮冷却）。"""
    try:
        d = load_rotation()
        return datetime.strptime(str(d.get("started") or "")[:19],
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp()
    except Exception:
        return 0.0


def _picked_in_round(ts: float | None = None) -> set:
    """本轮开轮之后已经被选过的目标 id。

    跟 consumed_curiosity_ids 的分工：那个是永久消费（好奇吃一条少一条），
    这个只算「本轮做过」——下一轮自动解冻，用来做练习/学习这类可重做的轮询。
    """
    ts = _round_started_ts() if ts is None else ts
    out: set = set()
    d = SELF / "iter_out"
    if not d.exists():
        return out
    for f in sorted(d.glob("decision_*.json"))[-120:]:
        try:
            if f.stat().st_mtime <= ts:
                continue
            j = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        gid = str(((j.get("pick") or {}) if isinstance(j, dict) else {}).get("id") or "")
        if gid:
            out.add(gid)
    return out


def load_practice_items() -> list:
    """练习池（第 7 池，2026-11-17 主人定）：实践/实验，补理论与实操的差距。

    独立文件 goals_practice.json，kind=practice。2026-11-17 主人口径改了两处：
      · 缺软件的、有软件的都要进池，都按池轮询；
      · 做过一次只冷却到下一轮（旧实现是 24h 硬冷却，跟轮次没关系）。
    练完的退出条件不变：评过分且最近一次剩余未解决为 0。
    """
    try:
        j = read_json(SELF / "goals_practice.json", {}) or {}
        items = j.get("items") or []
    except Exception:
        return []
    score = read_json(SELF / "practice" / "score.json", {}) or {}
    by_task = score.get("by_task") or {}
    picked = _picked_in_round()
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        gid = str(it["id"])
        sid = gid[5:] if gid.startswith("prac_") else None
        st = by_task.get(sid) if sid else None
        if (isinstance(st, dict) and int(st.get("rounds") or 0) > 0
                and int(st.get("last_open", st.get("open") or 0)) == 0):
            continue                      # 练完了：没剩未解决错误
        if gid in picked:
            continue                      # 本轮做过一次了，冷却到下一次轮询
        it.setdefault("kind", "practice")
        out.append(it)
    return out


def _pool_gen_ts() -> float | None:
    """池子自己记的生成时间（谁写的都算，不只看换轮戳）。"""
    try:
        j = json.loads(CURIOSITY_FILE.read_text(encoding="utf-8-sig"))
        s = str(j.get("generated_at") or "")[:19]
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp()
    except Exception:
        return None


def curiosity_rotate_stamp() -> float | None:
    """上一次换轮的真实时间 = max(换轮戳, 池子 generated_at)。

    11-16 那次池子 18:00 被写了、戳还停在 11-15 21:06（刷新不是走 refresh_curiosity），
    只认戳会让冷却和「今天换过几次」全按旧账算。
    """
    ts = 0.0
    try:
        j = json.loads(CURIOSITY_ROTATE_STAMP.read_text(encoding="utf-8-sig"))
        ts = float(j.get("ts_unix") or 0) or 0.0
    except Exception:
        ts = 0.0
    return max(ts, _pool_gen_ts() or 0.0) or None


def refresh_curiosity(force: bool = False) -> dict:
    """池子吃空 → 抓下一轮（带冷却，失败只记录不抛）。

    force=True 时无视冷却：池子文件本身空了 / 条目全过期时冷却没意义，
    不 force 的话会连着一整天「（不动）」空转。
    """
    last = curiosity_rotate_stamp()
    today = now().strftime("%Y-%m-%d")
    prev = {}
    try:
        prev = json.loads(CURIOSITY_ROTATE_STAMP.read_text(encoding="utf-8-sig"))
    except Exception:
        prev = {}
    n_today = int(prev.get("n_today") or 0) if prev.get("day") == today else 0
    _pg = _pool_gen_ts()
    if _pg and _pg > (float(prev.get("ts_unix") or 0) or 0.0):
        # 池子被别的入口换过（没盖本文件的戳），今天至少记一次，别让日上限变无限
        if datetime.fromtimestamp(_pg, CST).strftime("%Y-%m-%d") == today:
            n_today = max(n_today, 1)

    if not force:
        if last and (time.time() - last) < CURIOSITY_REFRESH_COOLDOWN_H * 3600:
            return {"ok": False, "reason": f"距上次换轮 {(time.time() - last) / 60:.0f}min"
                                           f" < {CURIOSITY_REFRESH_COOLDOWN_H * 60:.0f}min，先等"}
        if n_today >= CURIOSITY_REFRESH_DAILY_MAX:
            return {"ok": False, "reason": f"今天已换轮 {n_today} 次，达上限 {CURIOSITY_REFRESH_DAILY_MAX}"}
    try:
        r = subprocess.run([sys.executable, str(HERE / "curiosity.py")],
                           cwd=str(SELF.parent), capture_output=True, timeout=90)
        out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
        ok = r.returncode == 0
        # 成功失败都盖时间戳：失败也进冷却，免得网络断了每轮都去重试一遍
        CURIOSITY_ROTATE_STAMP.write_text(
            json.dumps({"ts_unix": time.time(), "ok": ok,
                        "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
                        "day": today, "n_today": n_today + 1,
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
        # 池子里不止好奇：灵感（kind=inspiration）与好奇同级，不许被压成 curiosity；
        # 学习轮（kind=explore，learn.py 按课程表追加）也要保住 kind——
        # 压成 curiosity 会进错池组、explored_today 也认不出来。
        _k = it.get("kind")
        g["kind"] = _k if _k in ("curiosity", "inspiration", "explore") else "curiosity"
        g.setdefault("cost_rmb", 0)
        out.append(g)
    return out


def _pick_by_prob(pool: list, T: float = 4.0):
    """按 softmax 概率从已排序的 pool 里抽一个目标，返回 (goal, 实际用的 T)。

    T 夹在 [1.5, 6]：校准出来的 T 现在被顶到 20，拿它当真分布会把差距抹平
    （差 20 分的两个目标概率接近 1:1），收敛就废了。低分项仍有小概率被选中，
    这是故意的 —— 保留探索，但主要靠分数。
    任何异常退回 pool[0]，不让抽样故障阻断出活。
    """
    if not pool:
        return None, T
    t = max(1.5, min(6.0, float(T)))
    try:
        from jev_calib import softmax as _sm
        ps = _sm([sc for sc, _ in pool], t)
        if len(ps) == len(pool) and sum(ps) > 0:
            return random.choices([g for _, g in pool], weights=ps, k=1)[0], t
    except Exception:
        pass
    return pool[0][1], t


def pick(goals: list, lv: str, gate_state: str, idle_h: float,
         streak: dict | None = None, no_explore: bool = False,
         quota: str = "unknown", wallet: float | None = None,
         history: dict | None = None, meta_ok: bool = True,
         optimize_ok: bool = True, curiosity_ok: bool = True,
         kind_streak_info: tuple | None = None,
         cat_streak_info: tuple | None = None) -> tuple[dict | None, str, bool]:
    """返回 (目标, 由头, 是否被余额卡住)。第三位给 collect_signals 分辨「穷」和「被拒」。"""
    ceiling = COST_CEILING[lv]
    if _on("money.cost_cap") and wallet is not None:
        ceiling = min(ceiling, SINGLE_ROUND_CAP_RMB, float(wallet))
    streak = streak or {}
    history = history or {}
    pool = []
    money_block = False
    vetoed: list = []  # 被 veto.txt 拦下的候选（id, 原因），落进 decision 方便事后看
    # 跑偏收口（主人 2026-11-17）：连续 3 轮低分→只准「修 + 验证」，不许再抽新选题。
    # 收口不是停机：修 bug / 写测试 / 只读巡检照跑。读不到状态就当作没跑偏。
    _frz = {"freeze": False}
    _bal = {"adj": {}}
    _dux = {"adj": {}}
    if _on("pick.direction"):
        try:
            import direction as _dir
            _frz = _dir.drift()
            _bal = _dir.balance()
            _dux = _dir.half_duplex()
        except Exception:
            _frz, _bal, _dux = {"freeze": False}, {"adj": {}}, {"adj": {}}
    FREEZE_KINDS = ("optimize", "meta", "practice", "maintain")
    _frz_skip: list = []  # 收口期被挡在外面的选题，池子空了再退回
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
        if _on("money.quota") and quota == "empty" and raw_cost > 0:
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
        score += value_bonus(g.get("kind"))  # 台账实测均分优先，样本不足退 VALUE_BONUS 先验
        if _on("pick.direction"):
            # 方向脑（2026-11-17）：direction.md 的方向分 + veto.txt 禁做通道
            # veto 命中直接出局，方向分只调序不判定。缺文件/报错一律放行，绝不因为方向脑挂掉停摆。
            try:
                import direction as _dir
                _dv = _dir.check(
                    f"{g.get('title') or g.get('goal') or ''} {g.get('note') or ''} "
                    f"{g.get('curiosity_cat') or ''}",
                    str(g.get("kind") or ""),
                )
                if _dv.get("verdict") == "veto":
                    vetoed.append((g.get("id"), _dv.get("reason")))
                    continue
                score += DIRECTION_W * float(_dv.get("boost") or 0.0)
                g["_dir_boost"] = _dv.get("boost")
                _st = _dir.stage_of(g.get("kind"))
                _sadj = float(_bal.get("adj", {}).get(_st, 0.0) or 0.0)
                _sd = _dir.side_of(g.get("kind"))
                _dadj = 0.0 if _sd == "*" else float(_dux.get("adj", {}).get(_sd, 0.0) or 0.0)
                if _sadj or _dadj:
                    score += DIRECTION_W * (_sadj + _dadj)
                    g["_stage"] = {"stage": _st, "side": _sd, "adj": round(_sadj + _dadj, 2)}
            except Exception as _e:
                g["_dir_err"] = str(_e)[:60]
        if _frz.get("freeze") and str(g.get("kind") or "") not in FREEZE_KINDS:
            # 收口期：新选题/灵感/好奇不进池。不直接丢，先存着，池子空了再退回
            g["_frozen"] = True
            _frz_skip.append((score, g))
            continue
        try:  # 收敛护栏：高分空转的 kind 与今天已被选过的 id 往后排（self/convergence.py）
            import convergence as _conv
            score += _conv.pick_penalty(g.get("kind"), g.get("id"))
        except Exception:
            pass
        if g.get("kind") == "maintain":
            score -= MAINTAIN_PENALTY  # 省力空转不再优先；巡检只在别的事都做不了时上
        # 空转奖励已删：原「闲久了给 maintain +4」等于奖励空转（主人 11-05 指令）
        if _on("pick.streak"):
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
        if not _on("pick.score"):
            # 打分关掉只留「优先级 - 成本」，其余调序一律不做
            score = float(g.get("priority", 5)) * 10 - cost * w_cost
        pool.append((score, g))
    if not pool and _frz_skip:
        # 收口期池子里全是新选题：宁可退回也不空转停机（方向脑不该造成停摆）
        pool = _frz_skip
        _frz_skip = []
        _frz["fallback"] = True
    if not pool:
        return None, (
            f"没有成本≤{round(ceiling,3)} 且合规的可做目标"
            f"（段位={lv}，闸门={gate_state}，池={quota}，余额={wallet}）"
        ), money_block
    # 组合级疲劳（主人 2026-11-17）：疲劳单位是「组合」不是单条——旧组合 24h 内不再出现，
    # 但只要比历史多出一条（1+2 vs 1+2+3）就算新组合，照常放行；缩水也算旧组合。
    # 只砍成员不改分数，砍了谁写进由头，事后能查。
    _fat_drops: list = []
    _cap_drops: list = []
    if _on("pick.fatigue"):
        try:
            import fatigue as _fat
            _rounds = SELF / "jev" / "rounds.jsonl"
            pool, _fat_drops = _fat.apply(pool, _fat.load_sets(_rounds))
            # 单条出场配额（主人 2026-11-17 选 A）：组合砍到只剩一条时那条会变台柱，用配额兜住
            pool, _cap_drops = _fat.apply_cap(pool, _fat.load_appearances(_rounds))
        except Exception:
            _fat_drops, _cap_drops = [], []
    pool.sort(key=lambda x: (-x[0], random.random()))  # 同分随机，避免固定顺序垄断
    # 概率化选择（2026-11-17）：以前只取 pool[0]，账本里记的 p_top 跟实际选谁无关，
    # 概率纯装饰，拟合出来的 T 也就没意义。现在按 softmax 抽样，p_top 才是真概率。
    _T = 4.0
    try:
        from jev_calib import load_calib as _lc
        _T = float(_lc().get("T", 6.0))
    except Exception:
        pass
    if _on("pick.softmax"):
        chosen, pick_T = _pick_by_prob(pool, _T)
    else:
        chosen, pick_T = pool[0][1], 0.0  # 关掉抽样就是纯取最高分，p_top 不再有概率含义
    if _on("pick.jev") and PICK_LOG_ENABLED:
        try:  # Jev 式记账：候选全集 + 概率落一行，等回执对账（self/jev_calib.py）
            from jev_calib import log_candidates as _jev_log
            _jev_log(pool, T=pick_T, chosen_id=str(chosen.get("id")),
                     write=not _picklog_off())
        except Exception:
            pass
    if os.environ.get("IC_PICK_DEBUG"):
        try:
            (SELF / "iter_out" / "pick_debug.json").write_text(json.dumps([
                {"score": round(float(sc), 1), "kind": g.get("kind"),
                 "cat": g.get("curiosity_cat") or g.get("cat"),
                 "title": (g.get("title") or g.get("keyword") or "")[:44]}
                for sc, g in pool], ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    return chosen, (
        f"价值序按台账 0-10 分回灌（人工标注权重 3 倍，样本不足回退 VALUE_BONUS 先验），"
        f"priority×10 - cost，收敛护栏扣分；"
        f"p_top 只作概率诊断，不参与挑选（连续命中降权、久未选中冒泡）"
        + (f"；组合疲劳砍 {len(_fat_drops)} 条" if _fat_drops else "")
        + (f"；出场配额歇 {len(_cap_drops)} 条" if _cap_drops else "")
    ), False


DEFAULT_GOALS = {
    "_note": "自迭代的候选池。cost_rmb=0 表示不烧钱；kind: maintain=维护 / explore=探索 / archive=归档",
    "goals": [
        {"id": "g1", "title": "巡检自迭代信号链（collect_signals → bio_signals → compute_pain）是否还在跑", "kind": "maintain", "cost_rmb": 0, "priority": 3},
        {"id": "g2", "title": "把当天对话归档进 self/log.md 与 changes.md", "kind": "archive", "cost_rmb": 0, "priority": 2},
        {"id": "g3", "title": "检查 cron 与进程健康（6199/6185、定时任务是否还在）", "kind": "maintain", "cost_rmb": 0, "priority": 3},
        {"id": "g4", "title": "学习轮：python learn.py 抽一条课程缺口（池里有未完成则先做那条），按 verify 联网查大纲、并入同方向技能或新建，配 3 道自测题", "kind": "explore", "cost_rmb": 0.2, "priority": 4},
        {"id": "g5", "title": "清 plugin_data 残留与旧备份", "kind": "maintain", "cost_rmb": 0, "priority": 1,
         "tier": "t4_irreversible", "needs": "口令 + 当场确认（删数据）"},
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true",
                    help="打印人类可读的一圈建议，不写任何文件（等于不带参数；"
                         "FACTS/CHANGELOG 里一直写 --show，之前实现漏了这个参数）")
    ap.add_argument("--commit", action="store_true", help="写文件并追加 log.md")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--round-start", default=None, metavar="LABEL",
                    help="一轮 agent 唤醒开始：记下上游真实余额")
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
    ap.add_argument("--rotation", action="store_true", help="看八池轮次到哪了")
    ap.add_argument("--rotation-reset", action="store_true", help="强制开新一轮（used 清空）")
    args = ap.parse_args()

    if args.show and args.commit:
        ap.error("--show 只看、--commit 落账，两个不能一起用")
    if args.show or not args.commit:
        # 只读模式不许往 rounds.jsonl 写：写进去的假「中选」会让 convergence
        # 把同一条 id 判成反复踩坑，直接扣到封顶（2026-11-17 修）
        # 2026-11-17 20:0x 再修：原来只挡了显式 --show，但 help 写着「不带参数等于 --show」，
        # 实际不带参数时 args.show=False、照样落盘 —— 抓到 20:03~20:04 每 5 秒多一行、
        # 归档轮次 r20→r21 一起往前跑。只有 --commit 才该落账，其余一律关闸。
        global PICK_LOG_ENABLED
        PICK_LOG_ENABLED = False
        # 子进程拿不到上面这个内存开关，走环境变量才拦得住（2026-11-17 补）
        os.environ["IC_PICK_LOG"] = "0"

    if args.round_start is not None:
        print(json.dumps(round_start(args.round_start), ensure_ascii=False))
        return 0
    if args.round_end:
        print(json.dumps(round_end(charge=not args.no_charge), ensure_ascii=False))
        return 0
    if args.rotation_reset:
        d = load_rotation()
        if d["used"]:
            d["history"].append({"round": d["round"], "used": sorted(d["used"]),
                                 "closed": now().strftime("%Y-%m-%d %H:%M:%S"), "forced": True})
        d["round"] = int(d.get("round", 1)) + 1
        d["used"] = []
        d["started"] = now().strftime("%Y-%m-%d %H:%M:%S")
        save_rotation(d)
        print(f"已开第 {d['round']} 轮，池子全部解锁")
        return 0
    if args.rotation:
        d = load_rotation()
        used = d["used"]
        print(f"第 {d['round']} 轮（起于 {d.get('started', '?')}）")
        for p in POOL_ORDER:
            mark = "✓跑过" if p in used else "待跑"
            extra = ""
            if (d.get("last") or {}).get("pool") == p:
                extra = f"  最近: {(d.get('last') or {}).get('ts', '')} {(d.get('last') or {}).get('title', '')[:24]}"
            print(f"  {mark}  {p}{extra}")
        if d["history"]:
            print(f"  历史轮次 {len(d['history'])} 条，最近关闭: {d['history'][-1]}")
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
        es = elapsed_stats()
        if es.get("n"):
            print(f"\n单轮时长（{es['n']} 轮，>5s 才算一轮）：中位 {es['median']}s  "
                  f"P75 {es['p75']}s  P90 {es['p90']}s  峰 {es['max']}s")
            print(f"  超 {es['limit_s']:.0f}s 的轮：{es['over_limit']} 个"
                  + (f"，最近：{es['worst']}" if es.get("worst") else ""))
            print("  口径：settled_at − ts。封包目标 ≤60s，超 90s 的轮该拆成两轮。")
        else:
            print("\n单轮时长：还没样本（run --round-start/--round-end 各一次就有了）")
        return 0

    goals_file = SELF / "goals.json"
    if not goals_file.exists():
        if args.commit:
            goals_file.write_text(json.dumps(DEFAULT_GOALS, ensure_ascii=False, indent=2), encoding="utf-8")
        goals = DEFAULT_GOALS["goals"]
    else:
        goals = read_json(goals_file, DEFAULT_GOALS).get("goals", [])
    if not _on("pool.goals"):
        goals = []  # 主池总开关关了：这一轮只剩好奇/自省这种临时池

    # 好奇候选只在内存里合并，不回写 goals.json（热点凉了自动消失）
    base_goals = list(goals)
    # 自省池（2026-11-17 加）：从 DANGLING/FACTS/alerts/changes 里推出来的活，
    # 同样只在内存里合并 —— 事情做完了源文件里那行没了，候选自动消失。
    intro_items = load_introspect_items() if _on("pool.introspect") else []
    base_goals = base_goals + intro_items
    # 优化池（2026-11-17 加）：加法律（补最小单测、拆超长函数）与减法律（去重、删无人调用）
    # 各自成候选；同样只在内存里合并，源没了候选自动消失。
    opt_items = load_optimize_items() if _on("pool.optimize") else []
    base_goals = base_goals + opt_items
    # 练习池（2026-11-17 第 7 池）：理论→实操，沙箱里开新工程；缺软件只申请不安装。
    prac_items = load_practice_items() if _on("pool.practice") else []
    base_goals = base_goals + prac_items
    # 维护池（2026-11-17 主人重定）：库 vs 现盘比对，丢一个功能一条候选。
    # goals.json 里的手写 maintain 条目只在审计模块挂了时才用（去重，避免两套混着）。
    mnt_items = load_maintain_items() if _on("pool.maintain") else []
    if mnt_items:
        base_goals = [g for g in base_goals if pool_of(g.get("kind")) != "maintain"]
        base_goals = base_goals + mnt_items
    # 归档池（2026-11-17 第 8 池料源）：当天上次归档之后的新轮次 → 一条增量归档任务。
    # 生成器有货时手写的 g2 归档条目让位（跟 maintain 同一套去重思路）。
    arc_items = load_archive_items() if _on("pool.archive") else []
    if arc_items:
        base_goals = [g for g in base_goals if pool_of(g.get("kind")) != "archive"]
        base_goals = base_goals + arc_items
    consumed = consumed_curiosity_ids()
    consumed_topics = consumed_curiosity_topics()
    all_cur = load_curiosity()

    def _fresh_cur(items: list, ids: set, topics: set) -> list:
        """挑没吃过的：候选 id 一次都不许重吃，同主题（跨天换 id）也不许重吃。"""
        return [g for g in items
                if str(g.get("id")) not in ids
                and _topic_key(g.get("keyword") or g.get("title")) not in topics]

    cur_items = (_fresh_cur(all_cur, consumed, consumed_topics)
                 if _on("pool.curiosity") else [])
    # 没有可吃的就起下一轮：全吃完走冷却，池子本身空/全过期则无视冷却立刻抓
    # 注意只看 kind=curiosity 的条目——灵感条目同池存着，不能顶替好奇的货
    rotate = None
    if _on("pool.curiosity") and not [g for g in cur_items if g.get("kind") == "curiosity"]:
        rotate = refresh_curiosity(force=not [g for g in all_cur if g.get("kind") == "curiosity"])
        consumed = consumed_curiosity_ids()
        consumed_topics = consumed_curiosity_topics()
        all_cur = load_curiosity()
        cur_items = _fresh_cur(all_cur, consumed, consumed_topics)
    goals = base_goals + cur_items
    # 缺料的池现场补一条（主人 2026-11-17：料源每次运行至少补一条；
    # 补得越少说明在空转，所以补了几条都如实记进 decision.refill，不造数）
    goals, refill = refill_pools(goals)

    pain, signals = compute_pain()
    pain_src = "self/health.json + bio_signals.json + 端口探针"
    if signals.get("stale"):
        pain_src += "（bio_signals 已过期，未计入）"
    lv = level_of(pain)
    last = last_log_ts(SELF / "log.md")
    idle_h = round((now() - last).total_seconds() / 3600, 2) if last else float("inf")
    balance = read_balance_rmb()
    g_state, g_ratio = gate(balance) if _on("money.gate") else ("ok", 1.0)

    wallet_bal, pay_days = payroll() if _on("money.payroll") else (wallet_state()[1], 0)
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

    # ---- 时序闭环：每个功能块跑完留一个 verdict，下一步只看上一步 ----
    trace: list[dict] = []

    def _step(name: str, state: str, detail: str = "") -> str:
        trace.append({"step": name, "state": state, "detail": str(detail)[:140]})
        return state

    _step("signal",
          "off" if not _on("signal.pain") else ("degraded" if signals.get("stale") else "ok"),
          f"pain={pain} stale={signals.get('stale', 0)} src={pain_src}")
    _step("money", g_state, f"bal={balance} wallet={wallet_bal} quota={quota_s}")

    # 八池轮转：本轮只留还没跑过的池；好奇开局单独跑一轮
    if _on("pool.rotation"):
        keep_pools, rot, rot_note = rotation_window(goals)
    else:
        keep_pools, rot, rot_note = set(), load_rotation(), "轮次开关关：全池参与"
    goals_rot = [g for g in goals if pool_of(g.get("kind")) in keep_pools] if keep_pools else goals

    def _pick(gs):
        return pick(gs, lv, g_state, 0 if idle_h == float("inf") else idle_h,
                    streak=streak, no_explore=explored_today(decisions),
                    quota=quota_s, wallet=wallet_bal, history=history,
                    meta_ok=meta_ok, optimize_ok=optimize_ok,
                    curiosity_ok=curiosity_ok, kind_streak_info=k_streak,
                    cat_streak_info=c_streak)

    goal, why, money_block = _pick(goals_rot)
    if goal is None and goals_rot and len(goals_rot) < len(goals):
        # 轮转窗口里全被门禁挡空（比如今天探索额度已用）——放宽到全池，别空转一整轮
        goal, why2, money_block = _pick(goals)
        if goal:
            why = "轮转池被门禁挡空，放宽到全池；" + why2
            rot_note += "（窗口挡空→放宽）"
    blocked = None
    if lv == "halt":
        goal, why, blocked = None, "pain≥85 停机待审，本轮只记录不执行", "halt"
    elif g_state == "stop":
        goal, why, blocked = None, f"余额{balance}rmb < 闸门{GATE_STOP_RMB}rmb，自主全停，只记录汇报", "gate"
    elif quota_s == "empty" and goal is None:
        goal, why, blocked = None, f"工资池见底（{wallet_bal}rmb），且没有零成本目标可做", "quota"
    elif goal is None and money_block:
        blocked = "money"

    _step("pool", "ok" if (goals_rot or goals) else "empty",
          f"aim={len(base_goals)} cur={len(cur_items)} intro={len(intro_items)} "
          f"window={len(keep_pools)}/{len(POOL_GROUPS)}")
    _step("pick", "ok" if goal else "none",
          ((goal or {}).get("title") or why) if goal else why)
    _step("exec", "ready" if goal else "skip",
          "按建议执行，走 round-start → round-end" if goal else (blocked or "无候选"))

    # 本轮的池：这里只做预览，真正记进轮次账在 --commit 且该轮真的落地之后
    pool_now = pool_of((goal or {}).get("kind"))
    used_preview = list(rot["used"])
    if pool_now and pool_now not in used_preview:
        used_preview.append(pool_now)
        used_preview.sort(key=lambda x: POOL_ORDER.index(x) if x in POOL_ORDER else 99)

    decision = {
        "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
        # 分块总览：哪条链路开着、哪几个功能块被关了（关掉的原因就是块没接上）
        "chains": _chains(),
        # 料源补料记账（2026-11-17 主人：补得越少越可能是空转；未正式跑前 history 少属正常，只看能不能真补出料）
        "refill": refill,
        # 时序闭环：signal→money→pool→pick→exec 每步的 verdict
        "trace": trace,
        "chain_stop": blocked or (None if goal else "no_candidate"),
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
        "rotation": {
            "round": rot["round"],
            "used": used_preview,
            "window": sorted(keep_pools),
            "note": rot_note,
            "pool": pool_now,
            "total_pools": len(POOL_GROUPS),
        },
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
        "introspect": {
            "pool": len(intro_items),
            "srcs": sorted({str(it.get("src", "")).split(":")[0] for it in intro_items} - {""}),
        },
        "optimize": {
            "pool": len(opt_items),
            "modes": sorted({str(it.get("mode", "")) for it in opt_items} - {""}),
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
        print(f"  轮次: {rot_note}｜已跑 {'/'.join(rot['used']) or '-'}（{len(rot['used'])}/{len(POOL_GROUPS)}）")
        _cs = decision.get("chains") or {}
        _line = " ".join(f"{c}{int(v.get('subs_on', 0))}/{int(v.get('subs_all', 0))}"
                         for c, v in _cs.items() if not str(c).startswith("_"))
        if _line:
            print(f"  开关: {_line}")
        _tr = " → ".join(f"{t['step']}:{t['state']}" for t in trace)
        print(f"  时序: {_tr}")
        print(f"  工资池: {wallet_bal}rmb（{quota_s}，本次发薪{pay_days}天） streak={streak or '无'}")
        if meta_ok:
            meta_ok_txt = "从未跑过" if meta_since is None else f"{meta_since}h"
            print(f"  自我优化轮: 可开（距上轮 {meta_ok_txt}，需≥{META_MIN_CANDIDATES} 个候选变体）")
        elif meta_pending:
            print(f"  自我优化轮: 被上一轮白改挡住（{meta_pending.get('ts')} 那条没有 used_by，先补用）")
        else:
            meta_txt = "从未跑过" if meta_since is None else f"{meta_since}h/{META_COOLDOWN_H}h"
            print(f"  自我优化轮: 关（距上轮 {meta_txt}，pain={pain}/{META_MAX_PAIN}）")
        if optimize_ok:
            opt_ok_txt = "从未跑过" if opt_since is None else f"{opt_since}h"
            print(f"  优化轮: 可开（距上次 {opt_ok_txt}，冷却 {OPTIMIZE_COOLDOWN_H}h）")
        else:
            opt_txt = "从未跑过" if opt_since is None else f"{opt_since}h/{OPTIMIZE_COOLDOWN_H}h"
            print(f"  优化轮: 关（距上次 {opt_txt}，或状态不允许）")
        print(f"  好奇池: 待吃 {len(cur_items)}/{len(all_cur)} 条"
              f"（{','.join(decision['curiosity']['cats']) or '空'}）"
              f" 已吃 {len(consumed)} → {'可开' if curiosity_ok else '关'}")
        print(f"  自省池: 待做 {len(intro_items)} 条"
              f"（{','.join(decision['introspect']['srcs']) or '空'}）")
        if _on("pool.maintain"):
            _sv = {}
            try:
                import maintain_audit as _ma
                _sv = _ma.survey()
            except Exception:
                pass
            if _sv:
                print(f"  维护池: 待做 {len(mnt_items)} 条｜库 {_sv['total']} 个脚本"
                      f" missing {len(_sv['missing'])} / changed {len(_sv['changed'])} /"
                      f" new {len(_sv['new'])}｜吻合分 {_sv['score']}")
            else:
                print(f"  维护池: 待做 {len(mnt_items)} 条｜审计模块读不到，用 goals.json 兜底")
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
                decision["rotation"]["used"] = list(rot["used"])
        if goal and pool_now and pool_now not in rot["used"] and _on("rotation.lock_on_commit"):
            rot["used"].append(pool_now)
            rot["used"].sort(key=lambda x: POOL_ORDER.index(x) if x in POOL_ORDER else 99)
            rot["last"] = {"pool": pool_now, "title": (goal or {}).get("title", ""),
                           "ts": now().strftime("%Y-%m-%d %H:%M:%S")}
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
        save_rotation(rot)
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
