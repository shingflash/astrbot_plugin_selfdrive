# -*- coding: utf-8 -*-
"""jev_ledger 自测（2026-11-17 重构后）：0-10 分制、人工权重 3 倍、样本不足回退先验。

跑法：python jev_selftest.py
全程在临时目录，不碰真实台账。
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

import jev_ledger as L


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jevledger_"))
    led = tmp / "ledger.jsonl"
    try:
        # 1) 档位
        assert L.band(0) == "空转", L.band(0)
        assert L.band(3) == "可能有用", L.band(3)
        assert L.band(5) == "可能有用", L.band(5)
        assert L.band(6) == "有用", L.band(6)
        assert L.band(10) == "有用", L.band(10)
        assert L.band(None) == "未标"

        # 2) 追加 + 同轮去重
        base = {"pool": "learn", "kind": "explore", "title": "学习：DSP 大纲",
                "analysis": "候选 4 个", "impl": "新建 skill", "result": "已并入"}
        L.append(dict(base, ts="2026-11-17 10:00:00", score=None), path=led)
        L.append(dict(base, ts="2026-11-17 10:00:00", score=4, by="self"), path=led)
        rows = L.read(led)
        assert len(rows) == 1, rows
        assert rows[0]["score"] == 4 and rows[0]["by"] == "self", rows[0]

        # 3) 人工覆盖自评
        L.mark("2026-11-17 10:00:00", 9, by="owner", path=led)
        rows = L.read(led)
        assert rows[0]["score"] == 9 and rows[0]["by"] == "owner", rows[0]
        # 自评再来一条不能盖掉人工
        L.append(dict(base, ts="2026-11-17 10:00:00", score=1, by="self"), path=led)
        assert L.read(led)[0]["score"] == 9, L.read(led)[0]

        # 4) 权重 3 倍：一条人工 9 + 三条自评 0 → (27+0)/(3+3)=4.5
        for i in range(3):
            L.append({"ts": f"2026-11-17 11:0{i}:00", "pool": "learn", "kind": "explore",
                      "title": f"空转{i}", "score": 0, "by": "self"}, path=led)
        st = L.stats(led)["learn"]
        assert st["n_h"] == 1 and st["n_s"] == 3, st
        assert abs(st["avg"] - 4.5) < 0.01, st
        assert abs(st["useful"] - 0.5) < 0.01, st   # 3/6 权重达标

        # 5) 回灌：向先验收缩（w=6 时 4.5 的实测被拉到 4.2；没样本直接返回先验）
        assert abs(L.bonus_for_pool("learn", prior=4.0, path=led) - 4.2) < 0.01
        assert L.bonus_for_pool("archive", prior=10.0, path=led) == 10.0
        assert abs(L.bonus_for_kind("explore", {"explore": 4}, path=led) - 4.2) < 0.01

        # 6) 待标 + 报告
        L.append({"ts": "2026-11-17 12:00:00", "pool": "curiosity", "kind": "curiosity",
                  "title": "好奇：某热搜", "analysis": "x", "impl": "y", "result": "z",
                  "score": None}, path=led)
        assert len(L.pending(path=led)) == 1
        txt = L.report(path=led)
        assert "learn" in txt and "4.5" in txt, txt

        # 7) 分数夹取
        assert L.clamp(13) == 10 and L.clamp(-2) == 0 and L.clamp(None) is None

        print("SELFTEST OK")
        print("  档位 0/3/5/6/10 正确；人工 3 倍权重均分 4.5；回灌与先验按阈值切换")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
