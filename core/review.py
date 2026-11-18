# -*- coding: utf-8 -*-
"""验收卡（2026-11-17 改成 0-10 台账口径）。

每条展示：池名 / 题目 / 轮次分析 / 实现方式 / 实现结果 / 标注分数。
主人回「序号 分数」即可，0=空转，1-5=可能有用，6-10=有用；人工标注权重 3 倍于自评。
用法：
  python review.py --next 5        # 最近 5 条待标注，输出卡片文本
  python review.py --label "3 8"   # 第 3 条打 8 分
  python review.py --label "3 0"   # 标空转
  python review.py --label "3 skip"
  python review.py --pending       # 还剩几条没标
"""

import difflib
import json
import re
import sys
from pathlib import Path

import jev_calib as jc
import jev_ledger as jl

_SELF_DIR = Path(__file__).resolve().parent
_CUR = None


def _curiosity_index():
    """curiosity.md -> [(标题, 落成/用处摘要)]，给卡片补「结果」用。"""
    global _CUR
    if _CUR is not None:
        return _CUR
    _CUR = []
    p = _SELF_DIR / "curiosity.md"
    if not p.exists():
        return _CUR
    text = p.read_text(encoding="utf-8", errors="replace")
    for seg in re.split(r"\n(?=## )", text)[1:]:
        head = seg.split("\n")[0].lstrip("# ").strip()
        body = ""
        m = re.search(r"（产出[：:]\s*([^）]+)）", head)
        if m:
            body = m.group(1).strip()
        m3 = re.search(r"落成的东西[：:]\s*([^\n]+)", seg)
        if m3:
            body = (body + "；" + m3.group(1).strip()).strip("；")
        if not body:
            m2 = re.search(r"【我能用到哪】(.*?)(?=\n\*\*【|\n来源|\Z)", seg, re.S)
            if m2:
                body = re.sub(r"\s+", " ", m2.group(1)).strip()
                body = body.replace("**", "")
        _CUR.append((head, body.strip()))
    return _CUR


def _decision_of(ts: str):
    """ts -> iter_out/decision_*.json（review 自己找，不依赖别的模块私有函数）。"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", str(ts))
    if not m:
        return None
    stem = "decision_" + "".join(m.groups()[:3]) + "_" + "".join(m.groups()[3:])
    hits = sorted((_SELF_DIR / "iter_out").glob(stem + "*.json"))
    if not hits:
        return None
    try:
        return json.loads(hits[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _kind_of(r: dict) -> str | None:
    cid = r.get("actual_id") or r.get("chosen")
    for o in r.get("options") or []:
        if str(o.get("id")) == str(cid):
            return o.get("kind")
    return None


def _result_line(r: dict, chosen: str) -> list:
    """每种环节都给自己那行的「结果」，好奇以外不许当成空转。"""
    kind = _kind_of(r)
    if kind and kind != "curiosity":
        d = _decision_of(r.get("ts")) or {}
        sc = (d.get("score") or {})
        out = [f"  结果：{kind} 轮（非好奇，按决策文件看）"]
        if sc.get("score") is not None:
            out.append(f"    -> 决策层 {float(sc['score']):.1f} 分")
        checks = sc.get("checks") or []
        bad = [c for c in checks if c.get("ok") is False]
        good = [c for c in checks if c.get("ok") is True]
        if bad:
            out.append("    -> 扣分项：" + "；".join(f"{c.get('id')}({c.get('detail','')})" for c in bad[:3]))
        elif good:
            out.append("    -> 全项过：" + "、".join(str(c.get("id")) for c in good[:4]))
        if not d:
            out.append("    -> 没找到决策文件，这轮可能没落盘")
        return out
    res = _result_of(chosen)
    if res:
        head, similar, body = res
        out = [f"  结果：curiosity.md『{head}』（像 {similar:.0%}）"]
        out.append(f"    -> {body[:150]}" if body else "    -> 笔记里没写「我能用到哪」")
        return out
    return ["  结果：curiosity.md 没有同主题笔记 —— 空转，只有标题"]


def _result_of(title: str):
    """actual_title -> (相近笔记标题, 相似度, 摘要)。没相近的返回 None。"""
    if not title:
        return None
    core = re.sub(r"^好奇[：:]", "", title).strip()
    core = re.sub(r"（[^）]*）", "", core).strip()
    best, score = None, 0.0
    for head, body in _curiosity_index():
        h = re.sub(r"^\d{4}-\d{2}-\d{2}\s*", "", head)
        h = re.sub(r"（.*?）", "", h).strip()
        s = difflib.SequenceMatcher(None, core, h).ratio()
        if s > score:
            best, score = (head, body), s
    if best and score >= 0.22:
        return best[0], score, best[1]
    return None


def pending():
    """待标注：台账里 score 还是空的轮次。"""
    return jl.pending()


def card(n: int = 5) -> str:
    try:  # 出卡片前先把已落盘的决策对回候选（所有环节一起补）
        jc.reconcile(quiet=True)
    except Exception:
        pass
    np_ = pending()
    if not np_:
        return "台账里没有待标注的轮次。"
    pack = list(enumerate(np_, 1))[-n:]
    lines = [f"待标注 {len(pack)} 条（总共 {len(np_)} 条没标）"]
    for i, r in pack:
        lines.append("")
        lines.append(f"[{i}] {r.get('ts')}  [{(r.get('pool') or '?')}/{(r.get('kind') or '?')}]")
        lines.append(f"  题目：{r.get('title') or '?'}")
        lines.append(f"  分析：{r.get('analysis') or '—'}")
        lines.append(f"  实现：{r.get('impl') or '—'}")
        lines.append(f"  结果：{r.get('result') or '—'}")
        lines.append(f"  标注：{jl.band(r.get('score'))}" +
                     ("" if r.get("by") in (None, "") else f"（{r.get('by')}）"))
    lines.append("")
    lines.append("回「序号 分数」打 0-10：0=空转，1-5=可能有用，6-10=有用；「序号 skip」跳过，可一次报多条。")
    return "\n".join(lines)


def apply(text: str) -> str:
    """解析 '3 8; 4 0; 5 skip' 这种批量标注（分数 0-10）。"""
    np_ = pending()
    done, bad = [], []
    chunks = [c.strip() for c in str(text).replace("，", ";").replace(",", ";").split(";") if c.strip()]
    for chunk in chunks:
        parts = chunk.split()
        if len(parts) < 2:
            bad.append(chunk)
            continue
        try:
            i = int(parts[0])
        except Exception:
            bad.append(chunk)
            continue
        if not (1 <= i <= len(np_)):
            bad.append(chunk)
            continue
        v = parts[1].lower()
        r = np_[i - 1]
        if v in ("skip", "s", "x"):
            done.append(f"[{i}] 跳过")
            continue
        try:
            s = float(v)
        except Exception:
            bad.append(chunk)
            continue
        if s < 0 or s > 10:
            bad.append(f"{chunk}（分数要 0-10）")
            continue
        hit = jl.mark(r.get("ts"), s, by="owner", pool=r.get("pool"))
        if hit is None:
            bad.append(f"{chunk}（台账里没找到这轮）")
        else:
            done.append(f"[{i}] {int(s)} 分（{jl.band(s)}）")
    msg = []
    if done:
        msg.append("已记：" + "、".join(done))
        msg.append(f"还剩 {len(pending())} 条待标")
    if bad:
        msg.append("没认出来：" + "、".join(bad))
    return "\n".join(msg) if msg else "什么都没记"


def main() -> int:
    argv = sys.argv[1:]
    if "--label" in argv:
        i = argv.index("--label")
        payload = " ".join(argv[i + 1:]) if len(argv) > i + 1 else ""
        print(apply(payload))
        return 0
    if "--pending" in argv:
        p = pending()
        print(f"待标注 {len(p)} 条" if p else "台账里没有待标注的轮次。")
        return 0
    n = 5
    if "--next" in argv:
        i = argv.index("--next")
        if len(argv) > i + 1 and argv[i + 1].isdigit():
            n = int(argv[i + 1])
    print(card(n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
