#!/usr/bin/env python3
"""DSH 账本读取（accounting.mjs 的 Python 移植，纯标准库，零依赖）。

原版在 dsh-whale-widget/lib/accounting.mjs，口径逐项照抄：
  SCALE=1e8、北京日期、active book、observedAmount（含 correction）、
  needsReview（有到账未核对就标待核对）、partialDay（观测缺口>10 分钟标部分数据）、
  另有历史记账本（换过 API key）与 legacy history 的回退。

只读 ~/.dsh/.dshw-usage.json，不写文件、不起进程、不调任何外部服务。
数字新鲜度取决于 DSH 客户端有没有在跑：lastAt 就是最后一次观测时间。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNTING_VERSION = 1
SCALE = 100_000_000
PARTIAL_GAP_MS = 10 * 60 * 1000
BEIJING = timezone(timedelta(hours=8))
DEFAULT_LEDGER = Path(os.path.expanduser("~")) / ".dsh" / ".dshw-usage.json"


# ---------- 金额 ----------

def money_units(value: float) -> int:
    return round(float(value) * SCALE)


def precise_money(value: float) -> float:
    return money_units(value) / SCALE


def units_to_money(units: int | float) -> float:
    return float(units or 0) / SCALE


# ---------- 日期 ----------

def beijing_day(ms: int | None = None) -> str:
    if ms is None:
        return datetime.now(BEIJING).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(float(ms) / 1000, BEIJING).strftime("%Y-%m-%d")


def day_start_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=BEIJING).timestamp() * 1000)


# ---------- 记账本 ----------

def _accounting(ledger: dict) -> dict | None:
    a = ledger.get("accounting")
    if not isinstance(a, dict) or a.get("version") != ACCOUNTING_VERSION:
        return None
    return a


def current_book(ledger: dict) -> dict | None:
    a = _accounting(ledger)
    if not a:
        return None
    books = a.get("books") or {}
    book = books.get(a.get("active"))
    return book if isinstance(book, dict) else None


def books_of(ledger: dict) -> list[dict]:
    a = _accounting(ledger)
    if not a:
        return []
    out = []
    for scope, book in (a.get("books") or {}).items():
        if not isinstance(book, dict) or not book.get("days"):
            continue
        out.append({"scope": scope, "book": book, "active": scope == a.get("active"),
                    "lastAt": int(book.get("lastAt") or 0)})
    return out


def observed_amount_units(row: dict) -> int:
    c = row.get("correction")
    debit = int(row.get("debitUnits") or 0)
    if c:
        return int(c.get("amountUnits") or 0) + debit - int(c.get("debitUnits") or 0)
    return debit


def revision_of(ledger: dict, row: dict) -> str:
    a = _accounting(ledger) or {}
    return ":".join(str(x or 0) for x in (
        a.get("active"), row.get("day"), row.get("firstAt"), row.get("lastUnits"),
        row.get("debitUnits"), row.get("creditUnits"), row.get("revision"),
    ))


# ---------- 部分数据判定 ----------

def partial_day_info(day: str, row: dict, now_ms: int | None = None) -> dict:
    now_ms = int(now_ms if now_ms is not None else datetime.now(BEIJING).timestamp() * 1000)
    try:
        start = day_start_ms(day)
    except Exception:
        return {"partialDay": True, "observedFromMs": None, "observedToMs": None,
                "leadingGapMs": None, "trailingGapMs": None}
    end = start + 86_400_000
    first = row.get("firstAt")
    last = row.get("lastAt")
    if not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
        return {"partialDay": True, "observedFromMs": first, "observedToMs": last,
                "leadingGapMs": None, "trailingGapMs": None}
    before = first - start
    after = (end - last) if now_ms >= end else 0
    return {
        "partialDay": before > PARTIAL_GAP_MS or after > PARTIAL_GAP_MS,
        "observedFromMs": first, "observedToMs": last,
        "leadingGapMs": max(0, before), "trailingGapMs": max(0, after),
    }


# ---------- 汇总 ----------

def balance_summary(ledger: dict, day: str | None = None, now_ms: int | None = None) -> dict | None:
    day = day or beijing_day()
    book = current_book(ledger)
    row = ((book or {}).get("days") or {}).get(day)
    if not row:
        return None
    correction = row.get("correction")
    credit = int(row.get("creditUnits") or 0)
    corrected_credit = int((correction or {}).get("creditUnits") or 0)
    needs_review = credit > corrected_credit
    source = "balance-needs-review" if needs_review else ("balance-corrected" if correction else "balance-observed")
    label = "已观测消费·待核对" if needs_review else ("已校正消费" if correction else "已观测消费")
    return {
        "day": day,
        "amount": units_to_money(observed_amount_units(row)),
        "currency": (book or {}).get("currency") or "CNY",
        "source": source,
        "label": label,
        "firstObservedAt": row.get("firstAt"),
        "lastObservedAt": row.get("lastAt"),
        "openingBalance": units_to_money(row.get("openingUnits")),
        "currentBalance": units_to_money(row.get("lastUnits")),
        "observedDecrease": units_to_money(row.get("debitUnits")),
        "observedIncrease": units_to_money(credit),
        "needsReview": needs_review,
        "revision": revision_of(ledger, row),
        "credits": units_to_money((correction or {}).get("creditsUnits")) if correction else None,
        "otherDebits": units_to_money((correction or {}).get("otherDebitsUnits")) if correction else None,
        "correctedAt": (correction or {}).get("at"),
        **partial_day_info(day, row, now_ms),
    }


def book_breakdown(ledger: dict, day: str) -> list[dict]:
    rows = []
    for item in books_of(ledger):
        book = item["book"]
        row = (book.get("days") or {}).get(day)
        if not row:
            continue
        rows.append({
            "scope": item["scope"], "currency": book.get("currency"), "active": item["active"],
            "lastAt": item["lastAt"], "amount": units_to_money(observed_amount_units(row)),
            "firstObservedAt": row.get("firstAt"), "lastObservedAt": row.get("lastAt"),
        })
    rows.sort(key=lambda r: (0 if r["active"] else 1, -r["lastAt"]))
    return rows


def day_summary(ledger: dict, day: str | None = None, now_ms: int | None = None) -> dict:
    day = day or beijing_day()
    observed = balance_summary(ledger, day, now_ms)
    events = sum(float(e.get("cost") or 0) for e in (ledger.get("events") or [])
                 if isinstance(e, dict) and e.get("day") == day)
    books = book_breakdown(ledger, day)
    others = [b for b in books if not b["active"]]
    other_same = [b for b in others if not observed or b["currency"] == observed["currency"]]
    if observed:
        out = dict(observed)
        out["eventEstimate"] = events
        out["bookBreakdown"] = books
        if other_same:
            out["otherBookAmount"] = sum(b["amount"] for b in other_same)
        return out
    if others:
        f = others[0]
        return {
            "day": day, "amount": f["amount"], "currency": f["currency"],
            "source": "balance-observed-other-account", "label": "已观测消费·历史账户",
            "firstObservedAt": f["firstObservedAt"], "lastObservedAt": f["lastObservedAt"],
            "needsReview": False, "eventEstimate": events, "bookBreakdown": books,
            **partial_day_info(day, {"firstAt": f["firstObservedAt"], "lastAt": f["lastObservedAt"]}, now_ms),
        }
    a = _accounting(ledger) or {}
    history = a.get("legacyHistory") or ledger.get("history") or {}
    h = history.get(day)
    has_history = isinstance(h, (int, float))
    return {
        "day": day, "amount": precise_money(h) if has_history else events,
        "currency": "CNY",
        "source": "legacy" if has_history else "events",
        "label": "旧版记录·未校正" if has_history else "本地估算",
        "partialDay": True, "eventEstimate": events, "bookBreakdown": books,
    }


# ---------- 读文件 + 对外快照 ----------

def load(path: str | os.PathLike | None = None) -> tuple[dict, Path]:
    p = Path(path).expanduser() if path else DEFAULT_LEDGER
    if not p.exists():
        raise FileNotFoundError(f"找不到 DSH 用量账本：{p}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f), p


def snapshot(path: str | os.PathLike | None = None, usd_cny: float = 7.1) -> dict:
    """一次取全：余额（本位币与折算 U）、今日已观测消费、新鲜度、是否部分数据。"""
    ledger, p = load(path)
    now_ms = int(datetime.now(BEIJING).timestamp() * 1000)
    today = beijing_day(now_ms)
    s = day_summary(ledger, today, now_ms)
    book = current_book(ledger) or {}
    last_at = (book.get("days") or {}).get(today, {}).get("lastAt") or book.get("lastAt")
    stale_s = round(max(0.0, (now_ms - float(last_at)) / 1000), 1) if last_at else None
    cny = float(s.get("amount") or 0)
    try:
        _t, _avg, _ratio = burn_ratio(path)
    except Exception:
        _avg, _ratio = 0.0, 0.0
    return {
        "path": str(p),
        "day": today,
        "currency": s.get("currency") or "CNY",
        "balance": round(float(s.get("currentBalance") or ledger.get("lastBalance") or 0), 2),
        "today_spent": round(cny, 2),
        "daily_avg": round(float(_avg), 2),
        "burn_ratio": round(float(_ratio), 3),
        "source": s.get("source"),
        "label": s.get("label"),
        "partial_day": bool(s.get("partialDay")),
        "needs_review": bool(s.get("needsReview")),
        "observed_at_ms": last_at,
        "stale_seconds": stale_s,
        "usd_cny": usd_cny,
        "balance_usd": round(float(s.get("currentBalance") or ledger.get("lastBalance") or 0) / usd_cny, 2),
    }


def burn_ratio(path: str | os.PathLike | None = None, lookback: int = 5) -> tuple[float, float, float]:
    """今日已观测消费 / 近 N 个完整日日均。返回 (今日, 日均, 比值)。"""
    ledger, _ = load(path)
    now_ms = int(datetime.now(BEIJING).timestamp() * 1000)
    today = beijing_day(now_ms)
    today_amt = float(day_summary(ledger, today, now_ms).get("amount") or 0)
    days = sorted(d for d in ((current_book(ledger) or {}).get("days") or {}) if d < today)
    hist = []
    for d in days[-lookback:]:
        s = balance_summary(ledger, d, now_ms)
        if s and not s.get("partialDay"):
            hist.append(float(s["amount"]))
    avg = sum(hist) / len(hist) if hist else 0.0
    return today_amt, avg, (today_amt / avg if avg > 0 else 0.0)


# ---------- 自己观测：直连 DeepSeek 余额 API（不依赖 DSH 客户端） ----------

BALANCE_URL = "https://api.deepseek.com/user/balance"


def fetch_balance(api_key: str, timeout: float = 12.0) -> dict:
    """GET /user/balance，返回 {currency, balance, available}。失败抛 RuntimeError 带状态码。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(BALANCE_URL, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"余额接口 HTTP {e.code}（401 = key 无效或不是 DeepSeek 平台 key）") from e
    except Exception as e:
        raise RuntimeError(f"余额接口请求失败：{type(e).__name__}: {e}") from e
    infos = data.get("balance_infos") or []
    if not infos:
        raise RuntimeError("余额接口没有返回 balance_infos")
    info = infos[0]
    return {
        "currency": (info.get("currency") or "CNY").upper(),
        "balance": float(info.get("total_balance") or 0),
        "granted": float(info.get("granted_balance") or 0),
        "topped_up": float(info.get("topped_up_balance") or 0),
        "available": bool(data.get("is_available", True)),
    }


def observe_balance(ledger: dict, balance: float, currency: str = "CNY",
                    at_ms: int | None = None, scope: str = "deepseek") -> dict:
    """accounting.mjs observeBalance 的移植：写一条观测，顺手算当日已用。"""
    at = int(at_ms if at_ms is not None else datetime.now(BEIJING).timestamp() * 1000)
    day = beijing_day(at)
    units = money_units(balance)
    currency = (currency or "CNY").upper()
    context = f"{scope}-{currency}"
    a = ledger.get("accounting")
    if not isinstance(a, dict) or a.get("version") != ACCOUNTING_VERSION:
        a = {"version": ACCOUNTING_VERSION, "active": context, "books": {},
             "migratedAt": at, "legacyHistory": dict(ledger.get("history") or {})}
        ledger["accounting"] = a
    a.setdefault("books", {})
    book = a["books"].get(context)
    if not isinstance(book, dict):
        book = a["books"][context] = {"currency": currency, "days": {}}
    last_at = book.get("lastAt")
    if last_at is not None and at <= last_at:      # 重复/乱序样本忽略
        return balance_summary(ledger, ledger.get("date") or day, at) or {}
    a["active"] = context
    days = book.setdefault("days", {})
    row = days.get(day)
    if not row:
        row = days[day] = {"day": day, "firstAt": at, "lastAt": at, "openingUnits": units,
                           "lastUnits": units, "debitUnits": 0, "creditUnits": 0,
                           "revision": 0, "correction": None}
    else:
        delta = int(row.get("lastUnits") or 0) - units
        if delta > 0:
            row["debitUnits"] = int(row.get("debitUnits") or 0) + delta
        if delta < 0:
            row["creditUnits"] = int(row.get("creditUnits") or 0) - delta
        row["lastUnits"] = units
        row["lastAt"] = at
    book["lastAt"] = at
    ledger["date"] = day
    ledger["dayStart"] = units_to_money(row["openingUnits"])
    ledger["lastBalance"] = units_to_money(row["lastUnits"])
    summary = balance_summary(ledger, day, at) or {}
    ledger["todayUsage"] = summary.get("amount")
    ledger.setdefault("history", {})[day] = summary.get("amount")
    return summary


def save(ledger: dict, path: str | os.PathLike | None = None) -> Path:
    p = Path(path).expanduser() if path else DEFAULT_LEDGER
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def refresh(path: str | os.PathLike | None = None, api_key: str | None = None) -> dict:
    """自己拉一次真实余额并写回账本。key 取自 DEEPSEEK_API_KEY / DSH_BALANCE_KEY。"""
    key = api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("DSH_BALANCE_KEY")
    if not key:
        raise RuntimeError("没有 DeepSeek 平台 API key（设 DEEPSEEK_API_KEY 或 DSH_BALANCE_KEY）")
    got = fetch_balance(key)
    ledger, p = load(path)
    before = float(ledger.get("lastBalance") or 0)
    summary = observe_balance(ledger, got["balance"], got["currency"])
    save(ledger, p)
    return {"balance": got["balance"], "currency": got["currency"],
            "before": before, "today_spent": summary.get("amount"),
            "partial_day": summary.get("partialDay"), "path": str(p)}


if __name__ == "__main__":
    import sys

    if "--refresh" in sys.argv:
        try:
            print(json.dumps(refresh(), ensure_ascii=False, indent=2))
        except Exception as e:
            print("刷新失败：", e)
        raise SystemExit(0)
    snap = snapshot()
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    t, avg, r = burn_ratio()
    print(f"今日 {t:.2f} / 日均 {avg:.2f} / 比值 {r:.2f}")
