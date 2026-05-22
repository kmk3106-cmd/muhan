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


def run() -> dict:
    """1회 감사 실행 + JSONL 1줄 append (스케줄러/엔드포인트 진입점)."""
    items = _audit_infinite()
    has_mismatch = any(it.get("status") == "mismatch" for it in items)
    has_fail = any(it.get("status") == "audit_failed" for it in items)
    overall = "audit_failed" if has_fail else ("mismatch" if has_mismatch else "ok")
    pt = {"ts": _now_kst_iso(), "overall": overall, "items": items}
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
