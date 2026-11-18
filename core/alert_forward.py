"""告警转发辅助脚本：只读 alerts.jsonl，按游标筛新行，更新游标。

用法:
    python self/alert_forward.py            # 只看，不动游标
    python self/alert_forward.py --commit   # 有新行才把游标推到最新

游标 last_ts 历史上出现过两种形态（epoch float 与 "YYYY-MM-DD HH:MM:SS" 字符串），
统一归一成 epoch 秒再比，避免字符串比较把新行判成旧的。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

W = Path(__file__).resolve().parent.parent
A = W / "alerts.jsonl"
C = W / "alerts_cursor.json"


def to_epoch(v):
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def main():
    commit = "--commit" in sys.argv
    cur = {}
    if C.exists():
        try:
            cur = json.loads(C.read_text(encoding="utf-8-sig")) or {}
        except Exception:
            cur = {}
    last = to_epoch(cur.get("last_ts")) or 0.0

    rows = []
    if A.exists():
        for line in A.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            r["_ts"] = to_epoch(r.get("ts"))
            rows.append(r)

    new = [r for r in rows if (r.get("_ts") or 0) > last]
    new.sort(key=lambda r: r["_ts"] or 0)
    out = {
        "total": len(rows),
        "new": len(new),
        "cursor": last,
        "items": [
            {"at": r.get("at") or datetime.fromtimestamp(r["_ts"]).strftime("%H:%M") if r.get("_ts") else "",
             "level": r.get("level"), "msg": r.get("msg")}
            for r in new[-8:]
        ],
    }
    if commit and new:
        top = max(r["_ts"] for r in new)
        cur["last_ts"] = top
        C.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        out["committed_to"] = top
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
