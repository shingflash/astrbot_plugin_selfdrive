# -*- coding: utf-8 -*-
"""收敛护栏的真实节奏仿真：不调模型、不写账本，只喂合成时间戳。

压测 50 轮是秒级连跑（57 秒跑完 50 轮），全落在同一个归并桶里，护栏本来就不会出手，
所以那 50 轮不能证明护栏有没有用。这个脚本按真实节奏重排时间戳（默认 90 秒/轮），
用当轮真实候选分数 + 收敛罚分做 softmax 抽样，看同一 id 会不会被反复中选。

跑法: python sim_guard.py [--gap 90] [--rounds 50] [--seed 7]
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import convergence as C

HERE = Path(__file__).resolve().parent
ROUNDS = HERE / "jev" / "rounds.jsonl"


def load_tail(n: int) -> list[dict]:
    rows = [json.loads(l) for l in ROUNDS.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[-n:]


def run(gap_s: int, rounds: int, seed: int) -> dict:
    rows = load_tail(rounds)
    base = datetime.now() - timedelta(seconds=gap_s * len(rows))
    seq = []
    for i, r in enumerate(rows):
        q = dict(r)
        q["ts"] = (base + timedelta(seconds=gap_s * i)).strftime("%Y-%m-%d %H:%M:%S")
        q.pop("chosen", None)
        q.pop("actual_id", None)
        q.pop("chosen_p", None)
        q.pop("p_top", None)
        seq.append(q)

    rng = random.Random(seed)
    picked: list[str] = []
    picks: list[dict] = []
    for i, cur in enumerate(seq):
        upto = seq[:i]                      # 只用「已经发生」的轮次判罚，不看未来
        scored = []
        for o in cur.get("options", []):
            gid, kind = str(o.get("id")), o.get("kind")
            pen = C.kind_penalty(kind, upto) + C.id_penalty(gid, upto)
            scored.append((float(o.get("score", 0.0)) + pen, gid, kind, pen))
        if not scored:
            continue
        T = float(cur.get("T") or 6.0)
        m = max(s for s, *_ in scored)
        weights = [pow(2.718281828, (s[0] - m) / T) for s in scored]
        tot = sum(weights)
        x = rng.random() * tot
        acc = 0.0
        chosen = scored[-1]
        for cand, ww in zip(scored, weights):
            acc += ww
            if x <= acc:
                chosen = cand
                break
        picked.append(chosen[1])
        # 关键：把「这一轮真选了什么」写回去，后面的轮次才看得见历史，
        # 否则 id_penalty 永远匹配不上（2026-11-17 修：先前 pop 掉 chosen 后忘了写回）
        seq[i]["chosen"] = chosen[1]
        seq[i]["actual_id"] = chosen[1]
        picks.append({"i": i, "ts": cur["ts"], "id": chosen[1], "kind": chosen[2],
                      "pen": round(chosen[3], 1), "score": round(chosen[0] - chosen[3], 1)})

    from collections import Counter
    c = Counter(picked)
    return {"rounds": len(picks), "distinct": len(c), "top": c.most_common(5),
            "max_repeat": c.most_common(1)[0][1] if c else 0, "picks": picks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=int, default=90, help="每轮间隔秒数（默认 90s）")
    ap.add_argument("--rounds", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = run(a.gap, a.rounds, a.seed)
    if a.json:
        print(json.dumps({k: v for k, v in r.items() if k != "picks"}, ensure_ascii=False))
        return 0
    print(f"仿真 {r['rounds']} 轮 / {a.gap}s 一轮 / seed={a.seed}")
    print(f"不同目标 {r['distinct']} 个，最高重复 {r['max_repeat']} 次")
    for gid, n in r["top"]:
        print(f"  {n:3d}x  {gid}")
    penalized = sum(1 for p in r["picks"] if p["pen"] <= -12)
    print(f"带罚分(-12 以下)中选的轮次: {penalized}/{r['rounds']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
