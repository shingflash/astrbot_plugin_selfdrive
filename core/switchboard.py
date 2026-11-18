# -*- coding: utf-8 -*-
"""开关中心：按「链路（总开关）+ 功能（子开关）」分发，最小单位是功能不是脚本。

链路按时序排：
  signal   采信号 → 算 pain → 定段位
  money    发薪 → 闸门 → 配额 → 单轮成本上限
  pool     goals/curiosity/introspect 装载 → 轮次窗口过滤
  pick     打分 → 连中降权 → 概率抽样 → 候选记账
  exec     round-start → 执行 → round-end 结账
  rotation used 记轮 → 全池跑完开新轮

用法：
  python switchboard.py                 # 看全部开关
  python switchboard.py --set pick.softmax off
  python switchboard.py --chain money off   # 整条链路关
  python switchboard.py --reset             # 恢复默认

查不存在的开关一律返回 True 且打一次警告 —— 宁可多跑一步，也不因为拼错名字把功能悄悄关掉。
"""
from __future__ import annotations

import json
import os
import sys

SELF = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(SELF, "switches.json")

# 默认值：只有两个「加了行为会变」的项默认关，其余保持现状
CHAINS = {
    "signal": {
        "title": "信号链 采信号→pain→段位",
        "subs": {
            "collect": ("采 bio_signals 入 pain", True),
            "pain": ("pain 参与段位判定", True),
            "stale_block": ("bio 过期时按保守值记 pain（不降级为 0）", False),
        },
    },
    "money": {
        "title": "资金链 发薪→闸门→配额→单轮上限",
        "subs": {
            "payroll": ("按天差发工资", True),
            "gate": ("余额闸门分级（no_explore/stop）", True),
            "quota": ("工资池见底只许零成本动作", True),
            "cost_cap": ("单轮成本上限（段位/单轮/钱包三者取小）", True),
        },
    },
    "pool": {
        "title": "池装载链 goals→curiosity→learn→inspiration→introspect→optimize→practice→maintain→archive→轮次窗口",
        "subs": {
            "goals": ("读 goals.json 主池", True),
            "curiosity": ("合并好奇池并触发换轮", True),
            "introspect": ("合并自省池", True),
            "optimize": ("合并优化池（加法/减法候选）", True),
            "practice": ("合并练习池（实操一次、解决/未解决错误计分）", True),
            "learn": ("合并学习池（按课程缺口抽一条，learn.py 落盘）", True),
            "inspiration": ("合并灵感池（info_pool 现成料 converge 一条）", True),
            "archive": ("合并归档池（当天上次归档之后的新轮次，一条一档）", True),
            "maintain": ("合并维护池（库 vs 现盘比对，丢一个功能一条候选）", True),
            "rotation": ("按八池轮次过滤候选", True),
        },
    },
    "pick": {
        "title": "选事链 打分→降权→抽样→记账",
        "subs": {
            "score": ("priority×10-cost 打分排序", True),
            "streak": ("连中降权 / 久未选中冒泡", True),
            "softmax": ("按 softmax 概率抽样（关则取最高分）", True),
            "jev": ("候选全集 + 概率落盘供对账", True),
            "direction": ("方向脑：读 direction.md 调序 + veto.txt 拦禁做", True),
            "fatigue": ("组合级疲劳：旧组合 24h 内不出现，加一条新条目算新组合", True),
        },
    },
    "exec": {
        "title": "执行链 round-start→执行→round-end",
        "subs": {
            "round_start": ("开轮记基线", True),
            "round_end": ("结账扣工资并写流水", True),
            "score_gate": ("未过评分不结账（先评后结）", False),
        },
    },
    "rotation": {
        "title": "轮次链 used→history→开新轮",
        "subs": {
            "lock_on_commit": ("commit 落地后才把池记进 used", True),
            "auto_close": ("六池全跑完自动开新一轮", True),
        },
    },
}

_warned: set[str] = set()

# 分块图：每块的输入 / 输出 / 落盘。拼装时只看接口，不看实现。
# 「关掉某块」等价于把它的输出接到上一块的输出（直通），所以下游不用改。
BLOCKS = {
    "signal.collect":   {"in": ["bio_signals.json", "health.json"], "out": ["sig"], "writes": []},
    "signal.pain":      {"in": ["sig"], "out": ["pain", "level"], "writes": ["health.json"]},
    "signal.stale_block": {"in": ["bio_signals.ts", "health.pain"], "out": ["pain_floor"], "writes": []},
    "money.payroll":    {"in": ["wallet.json"], "out": ["wallet_bal", "pay_days"], "writes": ["wallet.json"]},
    "money.gate":       {"in": ["balance"], "out": ["gate_state"], "writes": []},
    "money.quota":      {"in": ["wallet_bal"], "out": ["quota_state"], "writes": []},
    "money.cost_cap":   {"in": ["level", "wallet_bal"], "out": ["ceiling"], "writes": []},
    "pool.goals":       {"in": ["goals.json"], "out": ["base_goals"], "writes": []},
    "pool.curiosity":   {"in": ["goals_curiosity.json"], "out": ["cur_items"], "writes": ["goals_curiosity.json"]},
    "pool.introspect":  {"in": ["DANGLING/FACTS/alerts/changes"], "out": ["intro_items"], "writes": []},
    "pool.rotation":    {"in": ["pool_rotation.json", "goals"], "out": ["keep_pools"], "writes": []},
    "pool.learn":       {"in": ["learn_done.json", "skills/*/SKILL.md"], "out": ["learn_items"], "writes": ["goals_curiosity.json"]},
    "pool.inspiration": {"in": ["info_pool.json", "FACTS.md"], "out": ["insp_items"], "writes": ["goals_curiosity.json"]},
    "pool.archive":     {"in": ["iter_out/decision_*.json", "jev/rounds.jsonl"], "out": ["arc_items"], "writes": []},
    "pick.score":       {"in": ["goals", "ceiling"], "out": ["score"], "writes": []},
    "pick.streak":      {"in": ["decisions"], "out": ["score_delta"], "writes": []},
    "pick.softmax":     {"in": ["scored_pool"], "out": ["chosen", "p_top"], "writes": []},
    "pick.jev":         {"in": ["scored_pool", "chosen"], "out": ["candidates_log"], "writes": ["jev_candidates.jsonl"]},
    "exec.round_start": {"in": ["ledger"], "out": ["round_mark"], "writes": ["round_mark.json"]},
    "exec.round_end":   {"in": ["round_mark", "ledger"], "out": ["spent", "deducted"], "writes": ["wallet.json", "round_costs.jsonl"]},
    "exec.score_gate":  {"in": ["score"], "out": ["settle_ok"], "writes": []},
    "rotation.lock_on_commit": {"in": ["chosen", "pool"], "out": ["used"], "writes": ["pool_rotation.json"]},
    "rotation.auto_close":     {"in": ["used", "avail"], "out": ["round+1"], "writes": ["pool_rotation.json"]},
}


def _defaults() -> dict:
    return {c: {"on": True, "subs": {k: v[1] for k, v in spec["subs"].items()}}
            for c, spec in CHAINS.items()}


def _migrate(raw: dict) -> dict:
    """缺项用默认补齐；不认识的链/子项保留原样（人手工加的注释不丢）。"""
    out = _defaults()
    if not isinstance(raw, dict):
        return out
    for c, spec in CHAINS.items():
        node = raw.get(c)
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("on"), bool):
            out[c]["on"] = node["on"]
        subs = node.get("subs")
        if isinstance(subs, dict):
            for k in spec["subs"]:
                if isinstance(subs.get(k), bool):
                    out[c]["subs"][k] = subs[k]
            for k, v in subs.items():
                if k not in spec["subs"]:
                    out[c]["subs"][k] = v
    for c, node in raw.items():
        if c not in CHAINS:
            out[c] = node
    return out


def load(create: bool = False) -> dict:
    if not os.path.exists(FILE):
        d = _defaults()
        if create:
            save(d)
        return d
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            return _migrate(json.load(f))
    except Exception:
        return _defaults()


def save(d: dict) -> None:
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def on(path: str) -> bool:
    """path = "链.子项"；父链关掉则子项一律视为关。未知项返回 True。"""
    if "." not in path:
        return chain_on(path)
    c, sub = path.split(".", 1)
    spec = CHAINS.get(c)
    if spec is None or sub not in spec["subs"]:
        if path not in _warned:
            _warned.add(path)
            print("[switchboard] 未知开关 %s，按开处理" % path, file=sys.stderr)
        return True
    d = load()
    node = d.get(c) or {}
    if node.get("on") is False:
        return False
    return (node.get("subs") or {}).get(sub, True) is not False


def chain_on(chain: str) -> bool:
    spec = CHAINS.get(chain)
    if spec is None:
        if chain not in _warned:
            _warned.add(chain)
            print("[switchboard] 未知链路 %s，按开处理" % chain, file=sys.stderr)
        return True
    return (load().get(chain) or {}).get("on", True) is not False


def summary() -> dict:
    """给 decision 落盘用：链开关 + 生效子开关数。"""
    d = load()
    out = {}
    for c, spec in CHAINS.items():
        node = d.get(c) or {}
        con = node.get("on", True) is not False
        subs = node.get("subs") or {}
        enabled = [k for k in spec["subs"] if subs.get(k, spec["subs"][k][1]) is not False]
        out[c] = {"on": con, "subs_on": len(enabled) if con else 0,
                  "subs_all": len(spec["subs"]),
                  "off": sorted(k for k in spec["subs"] if k not in enabled)}
    return out


def set_one(path: str, val: bool) -> str:
    d = load(True)
    if "." in path:
        c, sub = path.split(".", 1)
        if c not in CHAINS or sub not in CHAINS[c]["subs"]:
            return "没有这个开关: %s" % path
        d[c]["subs"][sub] = val
    else:
        if path not in CHAINS:
            return "没有这条链路: %s" % path
        d[path]["on"] = val
    save(d)
    return "%s = %s" % (path, "on" if val else "off")


def show() -> None:
    d = load()
    for c, spec in CHAINS.items():
        node = d.get(c) or {}
        con = node.get("on", True) is not False
        print("%s %-9s %s" % ("●" if con else "○", c, spec["title"]))
        for k, (desc, dv) in spec["subs"].items():
            v = (node.get("subs") or {}).get(k, dv)
            mark = "on " if (v and con) else ("off" if not v else "父链关")
            print("    [%s] %-13s %s" % (mark, k, desc))
    off_all = [f"{c}.{k}" for c in CHAINS for k in CHAINS[c]["subs"]
               if not on(f"{c}.{k}")]
    print("\n当前关闭: %s" % (", ".join(off_all) if off_all else "无"))


def blocks_view() -> None:
    """分块表：每块一行，看接口就知道能不能拼到当前链上。"""
    for c, spec in CHAINS.items():
        con = chain_on(c)
        print("%s %s  %s" % ("●" if con else "○", c, spec["title"]))
        for k in spec["subs"]:
            b = BLOCKS.get(f"{c}.{k}") or {}
            state = "on " if on(f"{c}.{k}") else "off"
            print("    [%s] %-21s in=%-28s out=%-22s w=%s" % (
                state, k, ",".join(b.get("in") or []), ",".join(b.get("out") or []),
                ",".join(b.get("writes") or []) or "-"))
    missing = [f"{c}.{k}" for c in CHAINS for k in CHAINS[c]["subs"] if f"{c}.{k}" not in BLOCKS]
    print("\n没声明接口的块: %s" % (", ".join(missing) if missing else "无"))


def main() -> int:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print((__doc__ or "").strip())
        return 0
    if "--blocks" in args:
        blocks_view()
        return 0
    if "--reset" in args:
        save(_defaults())
        print("已恢复默认")
        return 0
    if "--set" in args:
        i = args.index("--set")
        if len(args) < i + 3:
            print("用法: --set 链路[.子项] on|off")
            return 2
        print(set_one(args[i + 1], args[i + 2].lower() in ("on", "1", "true", "yes")))
        return 0
    if "--chain" in args:
        i = args.index("--chain")
        if len(args) < i + 3:
            print("用法: --chain 链路名 on|off")
            return 2
        print(set_one(args[i + 1], args[i + 2].lower() in ("on", "1", "true", "yes")))
        return 0
    show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
