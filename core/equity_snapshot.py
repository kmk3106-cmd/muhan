# -*- coding: utf-8 -*-
"""Equity 시계열 스냅샷 (5 금일손익 · 11 MDD 용).

- 부모 스케줄러가 주기 호출. **KIS 미호출**: suite_metrics 가 이미 DB에서
  읽어둔 계좌·실현손익 값을 append-only JSONL 로 적재할 뿐이다.
- 누적 전(포인트<2)에는 None → 대시보드에서 "수집중" 표기.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _KST = ZoneInfo("Asia/Seoul")
except Exception:
    _KST = None

_FILE = Path(__file__).resolve().parent / "_equity.jsonl"


def _now_kst() -> datetime:
    return datetime.now(_KST) if _KST else datetime.now()


def snapshot() -> dict | None:
    """현재 계좌·전략 실현손익 1포인트 적재 (DB만 읽음)."""
    try:
        from .suite_metrics import _account, _cycles
        from .strategy_adapters import ADAPTERS
        strategies = list(ADAPTERS)
        accts = {k: _account(k) for k in strategies}
        canon, canon_ts = {}, ""
        for k in strategies:
            a = accts.get(k) or {}
            ts = str(a.get("updated_at") or "")
            if a and (canon == {} or ts > canon_ts):
                canon, canon_ts = a, ts
        realized = {}
        for k in strategies:
            cy = _cycles(k)
            realized[k] = cy.get("realized")
        pt = {
            "ts": _now_kst().isoformat(timespec="seconds"),
            "total_assets": canon.get("tot_evlu", 0),
            "cash": canon.get("cash", 0),
            "pnl": canon.get("pnl", 0),
            "realized": realized,
        }
        with _FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(pt, ensure_ascii=False) + "\n")
        return pt
    except Exception:
        return None


def _load() -> list[dict]:
    if not _FILE.exists():
        return []
    out = []
    try:
        for line in _FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        return []
    return out


def today_pnl() -> float | None:
    """금일손익 = 현재 총평가 − 오늘(KST) 최초 스냅샷 총평가."""
    pts = _load()
    if len(pts) < 2:
        return None
    today = _now_kst().strftime("%Y-%m-%d")
    todays = [p for p in pts if str(p.get("ts", "")).startswith(today)]
    if len(todays) < 2:
        return None
    base = todays[0].get("total_assets")
    last = todays[-1].get("total_assets")
    if base is None or last is None:
        return None
    return round(float(last) - float(base), 2)


def _mdd(series: list[float]) -> float | None:
    """최대낙폭(%) — peak 대비 최대 하락. 데이터 부족/peak<=0 시 None."""
    if len(series) < 2:
        return None
    peak = series[0]
    worst = 0.0
    for v in series:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak * 100.0
            if dd < worst:
                worst = dd
    return round(worst, 2)


def mdd_by_strategy() -> dict:
    """전략별 MDD(%) — 누적 실현손익 곡선 기준 (실현기준, 데이터 누적 시)."""
    pts = _load()
    res: dict = {}
    if len(pts) < 2:
        return res
    keys = set()
    for p in pts:
        keys |= set((p.get("realized") or {}).keys())
    for k in keys:
        ser = [float((p.get("realized") or {}).get(k) or 0) for p in pts]
        res[k] = _mdd(ser)
    return res


def account_mdd() -> float | None:
    """공용계좌 총평가자산 곡선 기준 MDD(%)."""
    pts = _load()
    ser = [float(p.get("total_assets") or 0) for p in pts]
    return _mdd(ser)
