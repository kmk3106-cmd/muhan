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
            "net_invested": canon.get("buy_amt", 0),
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


def _cycle_realized_by_date() -> dict:
    """전략별 {YYYYMMDD: 그날 실현손익 합}. CycleHistory.end_date·profit 기반(DB만)."""
    from .strategy_adapters import ADAPTERS
    import importlib
    out = {}
    for k in ADAPTERS:
        agg = {}
        try:
            from sqlalchemy import create_engine, select
            from sqlalchemy.orm import Session
            cfg = importlib.import_module(f"strategies.{k}.config")
            models = importlib.import_module(f"strategies.{k}.models")
            CH = models.CycleHistory
            with Session(create_engine(cfg.DATABASE_URL)) as s:
                rows = s.execute(select(CH.end_date, CH.profit)).all()
            for ed, pf in rows:
                d = str(ed or "").strip()
                if len(d) == 8 and d.isdigit():
                    agg[d] = agg.get(d, 0.0) + float(pf or 0)
        except Exception:
            agg = {}
        out[k] = agg
    return out


def _cf_at(cum, iso_date):
    """iso_date(YYYY-MM-DD) 시점 누적 입금/출금 (그 날짜 이하 최신)."""
    ymd = iso_date.replace("-", "")[:8]
    dep = wdr = 0.0
    for d, cb, cs in cum:
        if d <= ymd:
            dep, wdr = cb, cs
        else:
            break
    return dep, wdr


def _estimated_daily(first_real_date):
    """싸이클 실현이력으로 일별 추정 자산곡선(현재총자산 앵커, 실현기준 · 실제 MTM 아님).

    estimated_total(d) = 현재총자산 − 총실현 + (그날까지 누적실현)
    first_real_date(YYYY-MM-DD) 이전 날짜만 추정 포인트로 채움.
    """
    try:
        from .suite_metrics import _account
        from .strategy_adapters import ADAPTERS, active_rows
    except Exception:
        return [], {}
    canon, cts = {}, ""
    for k in ADAPTERS:
        a = _account(k) or {}
        ts = str(a.get("updated_at") or "")
        if a and (canon == {} or ts > cts):
            canon, cts = a, ts
    cur_total = float(canon.get("tot_evlu", 0) or 0)
    by = _cycle_realized_by_date()
    all_days = sorted({d for k in by for d in by[k]})
    if not cur_total or not all_days:
        return [], {}
    total_realized = sum(sum(by[k].values()) for k in by)
    seeds = {}
    for k in ADAPTERS:
        try:
            seeds[k] = sum(s for _, s in active_rows(k))
        except Exception:
            seeds[k] = 0
    base = cur_total - total_realized
    pts, cum = [], {k: 0.0 for k in by}
    sret_est = {k: [] for k in by}
    for d in all_days:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        if first_real_date and iso >= first_real_date:
            break
        for k in by:
            cum[k] += by[k].get(d, 0.0)
        pts.append({
            "ts": iso + "T00:00:00+09:00",
            "total_assets": round(base + sum(cum.values()), 2),
            "net_invested": round(base, 2),
            "cum_pnl": round(sum(cum.values()), 2),
            "est": True,
        })
        for k in by:
            sd = seeds.get(k) or 0
            sret_est[k].append(round(cum[k] / sd * 100, 2) if sd > 0 else 0.0)
    return pts, sret_est


def series(max_points: int = 400) -> dict:
    """차트용 시계열: 실측 스냅샷만 표시 (실측 시작일부터, 사용자 요청 2026-06).

    싸이클 기반 추정 소급(점선)은 제거 — 진짜 측정값(스냅샷)만 그린다.
    (_estimated_daily 는 보존하되 미사용.)
    """
    pts = _load()
    if len(pts) > max_points:
        step = len(pts) // max_points + 1
        pts = pts[::step] + [pts[-1]]
    try:
        from .strategy_adapters import active_rows, ADAPTERS
        seeds = {k: 0 for k in ADAPTERS}
        for k in ADAPTERS:
            try:
                seeds[k] = sum(s for _, s in active_rows(k))
            except Exception:
                seeds[k] = 0
    except Exception:
        seeds = {}
    # 추정 소급 제거: 실측 스냅샷만
    est_pts, est_sret = [], {}
    real_pts = [{
        "ts": p.get("ts"),
        "total_assets": float(p.get("total_assets") or 0),
        "net_invested": float(p.get("net_invested") or 0),
        "cum_pnl": float(p.get("pnl") or 0),
        "est": False,
    } for p in pts]
    keys = set(est_sret.keys())
    for p in pts:
        keys |= set((p.get("realized") or {}).keys())
    sret = {}
    for k in keys:
        b = seeds.get(k) or 0
        arr = list(est_sret.get(k, []))
        for p in pts:
            rv = float((p.get("realized") or {}).get(k) or 0)
            arr.append(round(rv / b * 100, 2) if b > 0 else 0.0)
        sret[k] = arr
    points = est_pts + real_pts
    # 실제 현금 입출금 원장(사용자 기록)의 일자별 누적값 부착
    try:
        from .cashflow_ledger import cumulative_by_date
        cum = cumulative_by_date()
    except Exception:
        cum = []
    if cum:
        for pt in points:
            dep, wdr = _cf_at(cum, str(pt.get("ts", ""))[:10])
            pt["deposit"] = dep
            pt["withdraw"] = wdr
    return {"points": points, "strategy_return": sret,
            "collecting": len(points) < 2, "has_estimate": bool(est_pts),
            "has_cashflow": bool(cum)}


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
