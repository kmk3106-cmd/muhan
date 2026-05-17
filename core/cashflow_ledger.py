# -*- coding: utf-8 -*-
"""실제 현금 입출금 원장 (사용자 기록).

계좌로의 실제 현금 입금/출금은 외부 거래라 거래데이터로 산출 불가 →
사용자가 직접 일자·금액을 기록한다. 차트의 입금액/출금액 라인은
이 원장의 일자별 누적값으로 그린다. (KIS 무호출·트레이딩 코어 무관)

저장: core/_cashflow.json  (배포별 보존 대상, gitignore)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_FILE = Path(__file__).resolve().parent / "_cashflow.json"


def _load() -> list:
    try:
        d = json.loads(_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(rows: list) -> None:
    _FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def list_entries() -> list:
    """[{id,date(YYYYMMDD),kind('deposit'|'withdraw'),amount,memo}] 일자순."""
    return sorted(_load(), key=lambda r: (str(r.get("date", "")), r.get("id", 0)))


def add_entry(date: str, kind: str, amount: float, memo: str = "") -> dict:
    d = "".join(ch for ch in str(date) if ch.isdigit())[:8]
    if len(d) != 8:
        raise ValueError("일자는 YYYYMMDD(또는 YYYY-MM-DD) 형식이어야 합니다.")
    if kind not in ("deposit", "withdraw"):
        raise ValueError("kind는 deposit 또는 withdraw 여야 합니다.")
    try:
        amt = round(float(amount), 2)
    except Exception:
        raise ValueError("금액이 올바르지 않습니다.")
    if amt <= 0:
        raise ValueError("금액은 0보다 커야 합니다.")
    rows = _load()
    rid = (max([r.get("id", 0) for r in rows], default=0) + 1) if rows else 1
    rec = {"id": rid, "date": d, "kind": kind, "amount": amt,
           "memo": str(memo or "")[:120], "ts": int(time.time())}
    rows.append(rec)
    _save(rows)
    return rec


def delete_entry(entry_id: int) -> bool:
    rows = _load()
    new = [r for r in rows if r.get("id") != int(entry_id)]
    if len(new) == len(rows):
        return False
    _save(new)
    return True


def cumulative_by_date() -> list:
    """정렬된 [(YYYYMMDD, 누적입금, 누적출금)] — 차트 부착용."""
    rows = list_entries()
    out, cd, cw = [], 0.0, 0.0
    by: dict = {}
    for r in rows:
        e = by.setdefault(r["date"], {"d": 0.0, "w": 0.0})
        if r["kind"] == "deposit":
            e["d"] += float(r["amount"] or 0)
        else:
            e["w"] += float(r["amount"] or 0)
    for d in sorted(by):
        cd += by[d]["d"]
        cw += by[d]["w"]
        out.append((d, round(cd, 2), round(cw, 2)))
    return out


def summary() -> dict:
    rows = list_entries()
    dep = round(sum(r["amount"] for r in rows if r["kind"] == "deposit"), 2)
    wdr = round(sum(r["amount"] for r in rows if r["kind"] == "withdraw"), 2)
    return {"total_deposit": dep, "total_withdraw": wdr,
            "net": round(dep - wdr, 2), "count": len(rows)}
