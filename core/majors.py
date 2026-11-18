# -*- coding: utf-8 -*-
"""专业粒度表（2026-11-17 主人要求：专业颗粒度对齐真实目录）。

原始数据在 data/ 下，纯 JSON，来源已在各文件 _meta 里写明：
  majors_benke.json     本科 883 种 / 93 专业类 / 13 门类（《本科专业目录（2026年）》）
  majors_zhijiao.json   高职专科 725 + 职业本科 247 + 中职 358（《职业教育专业目录（2021年）》+增补）
  units_yjs.json        研究生 117 一级学科 + 专业学位类别（《研究生教育学科专业目录（2022年）》）
本模块只做「读 + 归类 + 跟课程表对接」，不生成课程；课程仍在 curriculum_full.py。
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def _load(name: str):
    p = DATA / name
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


BENKE = _load("majors_benke.json")
_ZJ = _load("majors_zhijiao.json")
ZK = _ZJ.get("zhuanke", []) if isinstance(_ZJ, dict) else []
ZB = _ZJ.get("zhiben", []) if isinstance(_ZJ, dict) else []
ZZ = _ZJ.get("zhongzhi", []) if isinstance(_ZJ, dict) else []
YJS = _load("units_yjs.json")

MAJORS = BENKE + ZK + ZB + ZZ + YJS

# 层次 -> 真实目录口径（官方口径写死，方便对外报数时不自相矛盾）
OFFICIAL = {
    "本科": 883,
    "专科": 744,
    "职业本科": 247,
    "中职": 358,
    "一级学科": 117,
    "专业学位类别": 67,
}


def by_level() -> dict[str, int]:
    out: dict[str, int] = {}
    for m in MAJORS:
        out[m.get("level", "?")] = out.get(m.get("level", "?"), 0) + 1
    return out


def classes_of(level: str) -> dict[str, list[dict]]:
    """层次 -> {专业类: [专业...]}"""
    out: dict[str, dict[str, list[dict]]] = {}
    for m in MAJORS:
        if m.get("level") != level:
            continue
        d = out.setdefault(m.get("menlei") or m.get("dalei") or "未归类", {})
        d.setdefault(m.get("cls") or (m.get("name") if m.get("type") else "未分类"), []).append(m)
    return out


def _norm(s: str) -> str:
    return (s or "").replace("大类", "类").replace("学科类", "类").strip()


def course_key(cls: str, menlei: str) -> list[str]:
    """专业类映射到课程块 key（层次·门类·专业类）；同名类按门类区分。"""
    import curriculum as cf  # 公开版：课程表是平铺的 curriculum.py

    want = _norm(cls)
    hits = []
    for key in cf.CURRICULUM:
        lv, ml, cl = (key.split("·") + ["", "", ""])[:3]
        if _norm(cl) == want and (not menlei or ml == menlei or ml in menlei or menlei in ml):
            hits.append(key)
    if not hits:
        for key in cf.CURRICULUM:
            cl = (key.split("·") + ["", "", ""])[2]
            if _norm(cl) == want:
                hits.append(key)
    return hits


def coverage(level: str = "本科") -> dict:
    """这个层次里，有多少专业类已经挂上了课程。"""
    import curriculum as cf  # 公开版：课程表是平铺的 curriculum.py

    cls_map = classes_of(level)
    total_cls = sum(len(d) for d in cls_map.values())
    total_major = sum(len(lst) for d in cls_map.values() for lst in d.values())
    hit_cls = 0
    miss: list[str] = []
    for menlei, clss in cls_map.items():
        for cls in clss:
            if course_key(cls, menlei):
                hit_cls += 1
            else:
                miss.append(f"{menlei}·{cls}")
    return {
        "层次": level,
        "专业": total_major,
        "专业类": total_cls,
        "已挂课程的专业类": hit_cls,
        "未挂课程": miss,
        "课程块总数": len(cf.CURRICULUM),
    }


def stats() -> dict:
    return {
        "条目总数": len(MAJORS),
        "按层次": by_level(),
        "官方口径": OFFICIAL,
        "本科覆盖": coverage("本科"),
        "专科覆盖": coverage("专科"),
    }


if __name__ == "__main__":
    import sys

    print(json.dumps(stats(), ensure_ascii=False, indent=1))
