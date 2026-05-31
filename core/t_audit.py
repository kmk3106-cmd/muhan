# -*- coding: utf-8 -*-
"""무한매수법 T값 일일 감사 (read-only · DB만).

T값은 무한매수법 V2.2의 핵심 인자: T = (cum_buy − cum_sell) / B,
B = seed/A (1회매수액), 소수 둘째자리 올림 → 첫째자리.
매일 KST 09:00 부모 스케줄러가 호출하여 활성 포트폴리오별로:

  ① DB에 저장된 state.T (stored)
  ② cum 누계로 재계산한 T (T_recalc_cum)
  ③ 평단×수량/B 로 본 보유원가 T (T_from_holding)

세 값 일치 여부를 검증하고 불일치 시 사유를 분류·기록한다.
KIS API 호출 없음 — 코어 무수정. 저장: core/_t_audit.jsonl (append-only).
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _KST = ZoneInfo("Asia/Seoul")
except Exception:  # pragma: no cover
    _KST = None

_FILE = Path(__file__).resolve().parent / "_t_audit.jsonl"

# 비교 허용오차(소수 첫째자리 표기상 0.1 단위) — 반올림 차이는 통과
_TOL_STORED = 0.05   # state.T vs (cum-cum)/B
_TOL_HOLDING = 0.15  # cum 기준 vs 보유원가 기준 (체결 90일·기존보유분으로 약간 더 관대)


def _now_kst_iso() -> str:
    if _KST:
        return datetime.now(_KST).isoformat(timespec="seconds")
    return datetime.now().isoformat(timespec="seconds")


def _round_T(raw: float) -> float:
    """무한매수법 T 반올림: ceil(raw*10)/10 → 소수 첫째."""
    return round(math.ceil(raw * 10) / 10, 1)


def _audit_infinite() -> list[dict]:
    """무한매수법 활성 포트폴리오별 T 일치 검증."""
    out: list[dict] = []
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from strategies.infinite.config import DATABASE_URL
        from strategies.infinite.models import Portfolio, PortfolioState
    except Exception as e:  # 모듈 임포트 실패 — 치명
        return [{"strategy": "infinite", "ticker": "_import_error",
                 "status": "audit_failed", "reason": f"{type(e).__name__}: {e}"}]

    try:
        engine = create_engine(DATABASE_URL)
        SessionL = sessionmaker(bind=engine)
        with SessionL() as s:
            ports = s.scalars(
                select(Portfolio).where(Portfolio.is_active == True)  # noqa: E712
            ).all()
            for p in ports:
                st = s.scalar(
                    select(PortfolioState).where(PortfolioState.portfolio_id == p.id)
                )
                B = float(p.B)
                rec: dict = {
                    "strategy": "infinite",
                    "ticker": p.ticker,
                    "cycle": int(p.current_cycle or 1),
                    "seed": float(p.seed or 0),
                    "A": int(p.A or 40),
                    "B": round(B, 4),
                    "trading_enabled": bool(p.trading_enabled),
                }
                if not st or B <= 0:
                    rec.update({
                        "status": "no_state",
                        "reason": "PortfolioState 없음 또는 B=0 (seed/A 미설정)",
                    })
                    out.append(rec)
                    continue
                cum_b = float(st.cum_buy_amount or 0)
                cum_s = float(st.cum_sell_amount or 0)
                avg = float(st.avg_price or 0)
                qty = int(st.qty or 0)
                T_stored = round(float(st.T or 0), 1)
                T_recalc = _round_T(max(cum_b - cum_s, 0) / B)
                T_hold = _round_T(max(avg * qty, 0) / B)
                rec.update({
                    "avg_price": round(avg, 4),
                    "qty": qty,
                    "cum_buy": round(cum_b, 2),
                    "cum_sell": round(cum_s, 2),
                    "T_stored": T_stored,
                    "T_recalc_cum": T_recalc,
                    "T_from_holding": T_hold,
                })
                # ── 핵심 불변식 ──
                # 운영 T는 trading_logic.sync_state_from_api 369줄에서
                #   state.T = calc_T_from_avg(avg, qty, B)  (= 보유원가/B)
                # 로 계산·저장된다. 따라서 진짜 검증 대상은
                #   T_stored == T_from_holding  (sync가 최신 잔고로 돌았는가)
                # cum 기준 T와의 차이는 '부분매도 실현손익' 반영분이라 정상 — 결함 아님.
                reasons: list[str] = []
                if abs(T_stored - T_hold) > _TOL_STORED:
                    reasons.append(
                        f"DB stored T({T_stored})가 보유원가(평단×수량)/B 기준 운영 T({T_hold})와 불일치 "
                        f"— 잔고 변경 후 state.T 동기화 누락/DB stale 의심 (운영 T = 평단×수량/B)"
                    )
                rec["status"] = "ok" if not reasons else "mismatch"
                rec["reason"] = " | ".join(reasons)
                # 참고 메모(결함 아님): cum 기준 T가 보유원가 기준과 다르면 부분매도 실현손익 반영분
                note = ""
                if abs(T_recalc - T_hold) > _TOL_HOLDING:
                    realized = round((cum_b - cum_s) - avg * qty, 2)
                    note = (
                        f"참고: cum 기준 T({T_recalc})는 매도대금(실현손익 포함)을 빼므로 "
                        f"보유원가 기준({T_hold})과 ${realized} 차이 — 부분매도 정상 동작 (운영 T엔 미사용)"
                    )
                rec["note"] = note
                out.append(rec)
    except Exception as e:  # 통상 예외도 항목 1개로 남김
        out.append({
            "strategy": "infinite", "ticker": "_error",
            "status": "audit_failed", "reason": f"{type(e).__name__}: {e}",
        })
    return out


def _audit_cycle_integrity_infinite() -> list[dict]:
    """무한매수법 CycleHistory 각 행 정합 검증.

    end_date == 범위 내 마지막 매도일 / net qty = 0 / 매수·매도 합 일치 /
    profit = sell − buy / cycle_number 연속성.
    """
    out: list[dict] = []
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from strategies.infinite.config import DATABASE_URL
        from strategies.infinite.models import Portfolio, CycleHistory, Trade
    except Exception as e:
        return [{"strategy": "infinite", "scope": "_import_error",
                 "status": "audit_failed", "reason": f"{type(e).__name__}: {e}"}]
    try:
        engine = create_engine(DATABASE_URL)
        SessionL = sessionmaker(bind=engine)
        with SessionL() as s:
            pf_map = {p.id: p.ticker for p in s.scalars(select(Portfolio)).all()}
            cycles = s.scalars(
                select(CycleHistory).order_by(
                    CycleHistory.portfolio_id, CycleHistory.cycle_number
                )
            ).all()
            by_pf: dict = {}
            for cy in cycles:
                by_pf.setdefault(cy.portfolio_id, []).append(cy.cycle_number)
            for cy in cycles:
                ticker = pf_map.get(cy.portfolio_id, "?")
                trades = s.scalars(
                    select(Trade).where(
                        Trade.portfolio_id == cy.portfolio_id,
                        Trade.trade_date >= cy.start_date,
                        Trade.trade_date <= cy.end_date,
                    )
                ).all()
                buys = [t for t in trades if t.side == "buy"]
                sells = [t for t in trades if t.side == "sell"]
                buy_sum = round(sum(float(t.price) * int(t.qty) for t in buys), 2)
                sell_sum = round(sum(float(t.price) * int(t.qty) for t in sells), 2)
                buy_qty = sum(int(t.qty) for t in buys)
                sell_qty = sum(int(t.qty) for t in sells)
                last_sell = max((t.trade_date for t in sells), default="")
                problems: list[str] = []
                if last_sell and last_sell != cy.end_date:
                    problems.append(
                        f"end_date({cy.end_date}) ≠ 범위내 마지막 매도일({last_sell}) "
                        f"— /api/cycles 상세에 다음 싸이클 거래가 잘못 끼어들 수 있음"
                    )
                if buy_qty != sell_qty:
                    problems.append(
                        f"net qty {buy_qty - sell_qty} ≠ 0 (매수 {buy_qty}주 / 매도 {sell_qty}주) "
                        f"— 부분매도 후 종료 처리 의심"
                    )
                # cycle 1은 initial_holdings_cost가 포함될 수 있어 매수합계 검증 관대
                if cy.cycle_number != 1:
                    bdiff = round(buy_sum - float(cy.total_buy_amount or 0), 2)
                    if abs(bdiff) > 0.5:
                        problems.append(
                            f"매수합 trades=${buy_sum} vs history=${cy.total_buy_amount} (Δ${bdiff})"
                        )
                sdiff = round(sell_sum - float(cy.total_sell_amount or 0), 2)
                if abs(sdiff) > 0.5:
                    problems.append(
                        f"매도합 trades=${sell_sum} vs history=${cy.total_sell_amount} (Δ${sdiff})"
                    )
                expected_profit = round(
                    float(cy.total_sell_amount or 0) - float(cy.total_buy_amount or 0), 2
                )
                if abs(expected_profit - float(cy.profit or 0)) > 0.5:
                    problems.append(
                        f"profit ${cy.profit} ≠ sell−buy = ${expected_profit}"
                    )
                rec = {
                    "strategy": "infinite", "ticker": ticker,
                    "portfolio_id": cy.portfolio_id, "cycle": cy.cycle_number,
                    "start_date": cy.start_date, "end_date": cy.end_date,
                    "trades_count": len(trades),
                    "buys_count": len(buys), "sells_count": len(sells),
                    "buy_qty": buy_qty, "sell_qty": sell_qty,
                    "buy_sum": buy_sum, "sell_sum": sell_sum,
                    "history_buy": float(cy.total_buy_amount or 0),
                    "history_sell": float(cy.total_sell_amount or 0),
                    "history_profit": float(cy.profit or 0),
                    "last_sell_date": last_sell,
                    "status": "ok" if not problems else "mismatch",
                    "reason": " | ".join(problems),
                }
                out.append(rec)
            # cycle_number 연속성 (활성 포트폴리오만)
            active_ids = {p.id for p in s.scalars(
                select(Portfolio).where(Portfolio.is_active == True)  # noqa: E712
            ).all()}
            for pid in active_ids:
                nums = sorted(by_pf.get(pid, []))
                if not nums:
                    continue
                missing = [n for n in range(1, max(nums) + 1) if n not in nums]
                if missing:
                    out.append({
                        "strategy": "infinite", "ticker": pf_map.get(pid, "?"),
                        "portfolio_id": pid, "scope": "continuity",
                        "status": "mismatch",
                        "reason": f"cycle_number 누락: {missing} (보유 싸이클 {nums})",
                    })
    except Exception as e:
        out.append({"strategy": "infinite", "scope": "_error",
                    "status": "audit_failed", "reason": f"{type(e).__name__}: {e}"})
    return out


def _audit_cycle_integrity_ddsop() -> list[dict]:
    """떨사오팔 CycleHistory 각 행 정합 검증 (trades.cycle_number·ticker 기반)."""
    out: list[dict] = []
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from strategies.ddsop.config import DATABASE_URL as DDSOP_DB
        from strategies.ddsop.models import (
            Ticker as DdTicker, CycleHistory as DdCycle, Trade as DdTrade,
        )
    except Exception as e:
        return [{"strategy": "ddsop", "scope": "_import_error",
                 "status": "audit_failed", "reason": f"{type(e).__name__}: {e}"}]
    try:
        engine = create_engine(DDSOP_DB)
        SessionL = sessionmaker(bind=engine)
        with SessionL() as s:
            tk_map = {t.id: t.ticker for t in s.scalars(select(DdTicker)).all()}
            cycles = s.scalars(
                select(DdCycle).order_by(DdCycle.ticker_id, DdCycle.cycle_number)
            ).all()
            by_tk: dict = {}
            for cy in cycles:
                by_tk.setdefault(cy.ticker_id, []).append(cy.cycle_number)
            for cy in cycles:
                ticker = cy.ticker or tk_map.get(cy.ticker_id, "?")
                # ddsop trade는 cycle_number를 직접 가짐 → 더 정확한 기준
                trades = s.scalars(
                    select(DdTrade).where(
                        DdTrade.ticker == ticker,
                        DdTrade.cycle_number == cy.cycle_number,
                    )
                ).all()
                buys = [t for t in trades if t.side == "buy"]
                sells = [t for t in trades if t.side == "sell"]
                buy_sum = round(sum(float(t.amount or 0) for t in buys), 2)
                sell_sum = round(sum(float(t.amount or 0) for t in sells), 2)
                buy_qty = sum(int(t.qty or 0) for t in buys)
                sell_qty = sum(int(t.qty or 0) for t in sells)
                last_sell = max((t.trade_date for t in sells), default="")
                problems: list[str] = []
                if last_sell and last_sell != cy.end_date:
                    problems.append(
                        f"end_date({cy.end_date}) ≠ 마지막 매도일({last_sell})"
                    )
                if buy_qty != sell_qty:
                    problems.append(
                        f"net qty {buy_qty - sell_qty} ≠ 0 (매수 {buy_qty}주 / 매도 {sell_qty}주)"
                    )
                bdiff = round(buy_sum - float(cy.total_buy_amount or 0), 2)
                if abs(bdiff) > 0.5:
                    problems.append(
                        f"매수합 trades=${buy_sum} vs history=${cy.total_buy_amount} (Δ${bdiff})"
                    )
                sdiff = round(sell_sum - float(cy.total_sell_amount or 0), 2)
                if abs(sdiff) > 0.5:
                    problems.append(
                        f"매도합 trades=${sell_sum} vs history=${cy.total_sell_amount} (Δ${sdiff})"
                    )
                expected_profit = round(
                    float(cy.total_sell_amount or 0) - float(cy.total_buy_amount or 0), 2
                )
                if abs(expected_profit - float(cy.profit or 0)) > 0.5:
                    problems.append(
                        f"profit ${cy.profit} ≠ sell−buy = ${expected_profit}"
                    )
                rec = {
                    "strategy": "ddsop", "ticker": ticker,
                    "ticker_id": cy.ticker_id, "cycle": cy.cycle_number,
                    "start_date": cy.start_date, "end_date": cy.end_date,
                    "trades_count": len(trades),
                    "buys_count": len(buys), "sells_count": len(sells),
                    "buy_qty": buy_qty, "sell_qty": sell_qty,
                    "buy_sum": buy_sum, "sell_sum": sell_sum,
                    "history_buy": float(cy.total_buy_amount or 0),
                    "history_sell": float(cy.total_sell_amount or 0),
                    "history_profit": float(cy.profit or 0),
                    "last_sell_date": last_sell,
                    "status": "ok" if not problems else "mismatch",
                    "reason": " | ".join(problems),
                }
                out.append(rec)
            # ddsop는 활성 티커별 연속성 검사 — 단 초기화/재등록으로 max+1부터 시작하므로
            # 1번부터 연속이 아닌 게 정상 → 누락 검사는 보수적으로 (단순 max-len 비교)
            active_tids = {t.id for t in s.scalars(
                select(DdTicker).where(DdTicker.is_active == True)  # noqa: E712
            ).all()}
            for tid in active_tids:
                nums = sorted(by_tk.get(tid, []))
                if len(nums) >= 2:
                    gaps = [nums[i + 1] - nums[i] for i in range(len(nums) - 1)]
                    if any(g != 1 for g in gaps):
                        out.append({
                            "strategy": "ddsop", "ticker": tk_map.get(tid, "?"),
                            "ticker_id": tid, "scope": "continuity",
                            "status": "mismatch",
                            "reason": f"cycle_number 비연속 (보유 싸이클 {nums})",
                        })
    except Exception as e:
        out.append({"strategy": "ddsop", "scope": "_error",
                    "status": "audit_failed", "reason": f"{type(e).__name__}: {e}"})
    return out


def _section_overall(items: list[dict]) -> str:
    if any(it.get("status") == "audit_failed" for it in items):
        return "audit_failed"
    if any(it.get("status") == "mismatch" for it in items):
        return "mismatch"
    return "ok"


def run() -> dict:
    """전체 감사 1회 실행 — T값 + 싸이클 이력 정합 (스케줄러/엔드포인트 진입점)."""
    t_items = _audit_infinite()
    cyc_inf = _audit_cycle_integrity_infinite()
    cyc_dds = _audit_cycle_integrity_ddsop()
    cyc_items = cyc_inf + cyc_dds
    sections = {
        "t_value": {"overall": _section_overall(t_items), "items": t_items},
        "cycle_integrity": {"overall": _section_overall(cyc_items), "items": cyc_items},
    }
    sec_states = [sec["overall"] for sec in sections.values()]
    if "audit_failed" in sec_states:
        overall = "audit_failed"
    elif "mismatch" in sec_states:
        overall = "mismatch"
    else:
        overall = "ok"
    pt = {
        "ts": _now_kst_iso(), "overall": overall, "sections": sections,
        # 레거시 호환 — UI는 sections를 우선 사용
        "items": t_items,
    }
    try:
        with _FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(pt, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return pt


def history(limit: int = 30) -> list[dict]:
    """최근 N개 감사 결과 (시간 오름차순)."""
    if not _FILE.exists():
        return []
    try:
        lines = _FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    limit = max(1, min(int(limit or 30), 200))
    rows: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def latest() -> dict | None:
    h = history(limit=1)
    return h[-1] if h else None
