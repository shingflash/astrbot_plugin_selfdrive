# -*- coding: utf-8 -*-
"""课程表加载器（2.0.0 抽出来的私货位）。

原来课程表是写死的一个 35KB Python 常量（个人专业/课程清单）。公开版不带你自己的表，
改成读 local/curriculum.json，格式：
    {"水课": ["大学英语", "思修"],
     "专业": [{"层次": "本科", "门类": "工学", "专业类": "电子信息类",
               "专业": ["电子信息工程"],
               "课程": [{"名": "信号与系统", "级": 1}, {"名": "DSP", "级": 2}]}]}

文件不在 = 空表（learn 池会自己说"没有课程表"，不崩）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SELF = Path(os.environ.get("SELFDRIVE_HOME") or Path(__file__).resolve().parent)
WATER = {"水课", "体育", "军训", "思修", "马原", "毛概", "史纲", "形势与政策"}


def _load() -> list:
    p = SELF / "curriculum.json"
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    out = []
    for block in raw.get("专业") or []:
        item = {
            "层次": block.get("层次", ""),
            "门类": block.get("门类", ""),
            "专业类": block.get("专业类", ""),
            "专业": block.get("专业") or [],
            "课程": [],
        }
        for c in block.get("课程") or []:
            name = (c.get("名") or "").strip()
            if not name or name in WATER:
                continue
            item["课程"].append({"名": name, "级": int(c.get("级") or 1)})
        if item["课程"]:
            out.append(item)
    return out


CURRICULUM = _load()
