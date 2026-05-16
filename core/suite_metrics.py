# -*- coding: utf-8 -*-
"""통합 대시보드 15개 지표 집계 (읽기 전용).

원칙
- 신규 KIS 호출 0: 이미 워커가 동기화해 둔 DB 값만 읽는다(단일계좌 레이트리밋·레인 보호).
- 트레이딩 코어 무수정: 각 전략의 검증된 settings_store / 모델만 read.
- 단일 공용 계좌(69567573): 계좌지표는 가장 최신 스냅샷 1개만 사용(중복 합산 금지).
  전략지표(수익률·누적손익·보유)는 각 전략 CycleHistory/상태로 귀속.
- 5 금일손익 · 11 MDD 는 equity_snapshot 시계열에서 파생(누적 전까지 None).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .strategy_adapters import ADAPTERS, DISPLAY_NAMES

# 전략별 모듈 경로 (settings_store/config/models 재사용)
_PKG = {k: f"strategies.{k}" for k in ADAPTERS}


def _imp(strategy: str, mod: str):
    import importlib
    return importlib.import_module(f"{_PKG[strategy]}.{mod}")


def _account(strategy: str) -> dict:
    """검증된 settings_store.get_account_summary 재사용 (전체 공용계좌 스냅샷)."""
    try:
        cfg = _imp(strategy, "config")
        ss = _imp(strategy, "settings_store")
        a = ss.get_account_summary(cfg.DATABASE_URL) or {}
    except Exception:
        a = {}
    if not a:
        return {}
    return {
        "tot_evlu": float(a.get("tot_asst_amt", a.get("tot_evlu", 0)) or 0),
        "stock_evlu": float(a.get("stock_evlu", 0) or 0),
        "cash": float(a.get("cash", 0) or 0),
        "buy_amt": float(a.get("buy_amt", 0) or 0),
        "pnl": float(a.get("pnl", 0) or 0),
        "pnl_rt": float(a.get("pnl_rt", 0) or 0),
        "exrt": float(a.get("exrt", 0) or 0),
        "updated_at": a.get("updated_at"),
    }


def _engine(strategy: str):
    from sqlalchemy import create_engine
    cfg = _imp(strategy, "config")
    return create_engine(cfg.DATABASE_URL)


def _invested(strategy: str) -> float:
    """전략 투입금액 = 활성 종목 per-ticker 시드 합 (KIS 무호출)."""
    try:
        from .strategy_adapters import active_rows
        return round(sum(seed for _, seed in active_rows(strategy)), 2)
    except Exception:
        return 0.0


def _cycles(strategy: str) -> dict:
    """전략별 실현손익(완료 싸이클): Σprofit, 수익률, 건수, 승률."""
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        models = _imp(strategy, "models")
        CH = models.CycleHistory
        with Session(_engine(strategy)) as s:
            rows = s.execute(select(CH.profit, CH.total_buy_amount)).all()
        profit = round(sum(float(p or 0) for p, _ in rows), 2)
        buy = sum(float(b or 0) for _, b in rows)
        inv = _invested(strategy)
        # 수익률: 투입금액 기준(없으면 매수금액 기준 폴백)
        base = inv if inv > 0 else buy
        pct = round(profit / base * 100, 2) if base > 0 else 0.0
        wins = sum(1 for p, _ in rows if float(p or 0) > 0)
        win_rate = round(wins / len(rows) * 100, 1) if rows else None
        return {"realized": profit, "realized_pct": pct, "cycles": len(rows),
                "win_rate": win_rate, "invested": inv}
    except Exception:
        return {"realized": None, "realized_pct": None, "cycles": 0,
                "win_rate": None, "invested": _invested(strategy)}


def _holdings(strategy: str) -> dict:
    """전략별 보유종목 수 + 종목별 (수량·평단·매입원가). 현재가 미사용(KIS 무호출)."""
    items: list[dict] = []
    try:
        from sqlalchemy import select, func
        from sqlalchemy.orm import Session
        models = _imp(strategy, "models")
        with Session(_engine(strategy)) as s:
            if strategy == "infinite" or hasattr(models, "PortfolioState"):
                P, PS = models.Portfolio, models.PortfolioState
                pfs = s.scalars(select(P).where(P.is_active == True)).all()
                for p in pfs:
                    st = s.scalars(
                        select(PS).where(PS.portfolio_id == p.id)
                        .order_by(PS.synced_at.desc()).limit(1)
                    ).first()
                    qty = int(getattr(st, "qty", 0) or 0) if st else 0
                    if qty > 0:
                        avg = float(getattr(st, "avg_price", 0) or 0)
                        items.append({"ticker": p.ticker, "qty": qty,
                                      "avg_price": round(avg, 4),
                                      "cost": round(avg * qty, 2)})
            else:  # ddsop: Tranche BOUGHT 집계
                Tk, Tr = models.Ticker, models.Tranche
                TS = models.TrancheStatus
                bought = TS.BOUGHT.value if hasattr(TS, "BOUGHT") else "BOUGHT"
                tickers = s.scalars(select(Tk).where(Tk.is_active == True)).all()
                for tk in tickers:
                    trs = s.scalars(
                        select(Tr).where(Tr.ticker_id == tk.id,
                                         Tr.status == bought)
                    ).all()
                    qty = sum(int(t.qty or 0) for t in trs)
                    if qty > 0:
                        cost = sum(float(t.avg_price or 0) * int(t.qty or 0) for t in trs)
                        items.append({"ticker": tk.ticker, "qty": qty,
                                      "avg_price": round(cost / qty, 4) if qty else 0,
                                      "cost": round(cost, 2)})
    except Exception:
        pass
    return {"count": len(items), "items": items}


def _recent_trades(strategy: str, n: int = 12) -> list[dict]:
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        models = _imp(strategy, "models")
        T = models.Trade
        with Session(_engine(strategy)) as s:
            rows = s.scalars(select(T).order_by(T.id.desc()).limit(n)).all()
        out = []
        for t in rows:
            out.append({
                "strategy": strategy,
                "display_name": DISPLAY_NAMES.get(strategy, strategy),
                "trade_date": getattr(t, "trade_date", ""),
                "ticker": getattr(t, "ticker", ""),
                "side": getattr(t, "side", ""),
                "order_type": getattr(t, "order_type", ""),
                "price": float(getattr(t, "price", 0) or 0),
                "qty": int(getattr(t, "qty", 0) or 0),
                "amount": float(getattr(t, "amount", 0) or 0),
            })
        return out
    except Exception:
        return []


def _errors(strategy: str, n: int = 8) -> dict:
    """주문/API 오류상태: 최근 ERROR/WARNING 로그 + kill_switch."""
    logs: list[dict] = []
    kill = False
    try:
        cfg = _imp(strategy, "config")
        from pathlib import Path
        kill = Path(str(cfg.KILL_SWITCH_FILE)).exists()
    except Exception:
        pass
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        models = _imp(strategy, "models")
        L = models.AppLog
        with Session(_engine(strategy)) as s:
            rows = s.scalars(
                select(L).where(L.level.in_(["ERROR", "WARNING"]))
                .order_by(L.id.desc()).limit(n)
            ).all()
        for l in rows:
            ca = getattr(l, "created_at", None)
            logs.append({
                "strategy": strategy,
                "level": l.level,
                "message": (l.message or "")[:240],
                "created_at": ca.isoformat() if hasattr(ca, "isoformat") else str(ca or ""),
            })
    except Exception:
        pass
    return {"kill_switch": kill, "logs": logs}


def build_metrics() -> dict:
    """15개 지표 집계 결과."""
    strategies = list(ADAPTERS)
    accts = {k: _account(k) for k in strategies}
    # 공용계좌: 가장 최신 updated_at 스냅샷 1개를 전체계좌값으로 (중복 합산 금지)
    canon, canon_ts = {}, ""
    for k in strategies:
        a = accts.get(k) or {}
        ts = str(a.get("updated_at") or "")
        if a and (canon == {} or ts > canon_ts):
            canon, canon_ts = a, ts

    tot = canon.get("tot_evlu", 0)
    cash = canon.get("cash", 0)
    pnl = canon.get("pnl", 0)

    per = []
    realized_all = 0.0
    for k in strategies:
        cy = _cycles(k)
        hd = _holdings(k)
        er = _errors(k)
        if cy.get("realized") is not None:
            realized_all += cy["realized"]
        per.append({
            "strategy": k,
            "display_name": DISPLAY_NAMES.get(k, k),
            "kill_switch": er["kill_switch"],
            "invested": cy.get("invested", 0.0),       # 투입금액(시드합)
            "realized_pnl": cy["realized"],            # 전략누적손익(실현)
            "return_pct": cy["realized_pct"],          # 전략수익률
            "win_rate": cy.get("win_rate"),            # 승률
            "cycles": cy["cycles"],
            "holdings_count": hd["count"],             # 보유종목수
            "holdings": hd["items"],                   # 보유종목별(평단·원가)
            "errors": er["logs"],                      # 오류
        })

    # MDD : equity 시계열에서 (금일손익은 사용자 요청으로 제거)
    try:
        from .equity_snapshot import mdd_by_strategy, account_mdd
        mdd = mdd_by_strategy()
        acc_mdd = account_mdd()
    except Exception:
        mdd, acc_mdd = {}, None
    for p in per:
        p["mdd_pct"] = mdd.get(p["strategy"])           # 전략 MDD
    active_strats = sum(1 for p in per if not p["kill_switch"])

    trades = []
    for k in strategies:
        trades += _recent_trades(k, 12)
    trades.sort(key=lambda x: (x.get("trade_date", ""),), reverse=True)
    trades = trades[:20]

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "account": {                                   # 공용계좌(중복합산X)
            "total_assets": tot,                       # 1 총평가자산
            "net_invested": canon.get("buy_amt", 0),   # 2 순투입(매입원금)
            "total_pnl": pnl,                          # 3 총손익(평가)
            "total_return_pct": canon.get("pnl_rt", 0),# 4 총수익률
            "realized_pnl": round(realized_all, 2),    # 6 실현손익(전 전략 Σ)
            "unrealized_pnl": pnl,                     # 7 미실현(평가손익)
            "cash": cash,
            "cash_ratio": round(cash / tot * 100, 2) if tot else None,  # 8 현금비중
            "mdd_pct": acc_mdd,                         # 11 계좌 MDD
            "snapshot_at": canon.get("updated_at"),
        },
        "automation": {                                 # 자동매매 상태
            "active": active_strats,
            "total": len(per),
            "running": active_strats > 0,
        },
        "strategies": per,                              # 9~13,15
        "recent_trades": trades,                        # 14 매매로그
    }
