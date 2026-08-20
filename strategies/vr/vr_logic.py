# -*- coding: utf-8 -*-
"""VR 5.0 산출 로직 — 라오어 팬딩 표 역산·전수 검증 공식 (2026-08 확정).

검증 근거 (docs/VR_밸류리밸런싱_사양.md §6.9):
- 거치식 VR 0기 256주차: V 30905.34·밴드·매수 8단·매도 11단 전부 재현
- 인출식 VR 5기 47주차: V 15015.07·밴드·매수 8단(한도 10% 정지)·매도 15단 재현
  (유일 예외: 5기 매수점2 1센트 — 라오어 엑셀 반올림 누적 → 대조 허용오차 ±$0.01)

공식:
  ① 다음V   = V₁ + Pool/G + (E − V₁)/(2√G) + 현금흐름(적립 +, 인출 −)
  ② Pool이월 = 직전 마지막 Pool + 현금흐름
  ③ 밴드     = V×0.85 (하단) / V×1.15 (상단)
  ④ 매수점k  = 하단 ÷ (잔여 + 단위×(k−1)) — 누적 매수금 ≤ Pool×매수한도% 까지
  ⑤ 매도점k  = 상단 ÷ (잔여 − 단위×(k−1)) — 단수는 설정값(라오어 표는 표시 단수)
가격은 모델 수치(모델 잔여·모델 Pool)로 산출하며 배수와 무관. 주문 수량만 ×배수.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP


def r2(x: float) -> float:
    """엑셀식 반올림(half-up) 소수 2자리."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def next_v(v1: float, pool: float, g: float, e: float, cashflow: float = 0.0) -> float:
    """다음 V. cashflow: 적립식 +적립금 / 인출식 −인출금 / 거치식 0."""
    if g <= 0:
        raise ValueError("G must be > 0")
    return r2(v1 + pool / g + (e - v1) / (2 * math.sqrt(g)) + cashflow)


def bands(v: float) -> tuple[float, float]:
    """(하단, 상단) = (V×0.85, V×1.15)."""
    return r2(v * 0.85), r2(v * 1.15)


def buy_ladder(band_lo: float, model_qty: int, unit: int, pool: float,
               buy_limit_pct: float, max_steps: int = 40) -> list[dict]:
    """매수 사다리. 누적 매수금이 Pool×한도%를 넘기 직전까지 생성.

    반환 행: {step, price, qty_model, remain_after(모델), pool_after(모델)}
    """
    rows: list[dict] = []
    cap = pool * buy_limit_pct / 100.0
    spent = 0.0
    p_run = pool
    for k in range(max_steps):
        denom = model_qty + unit * k
        if denom <= 0:
            break
        price = r2(band_lo / denom)
        cost = unit * price
        if spent + cost > cap + 1e-9:
            break
        spent = r2(spent + cost)
        p_run = r2(p_run - cost)
        rows.append({
            "step": k + 1,
            "price": price,
            "qty_model": unit,
            "remain_after": model_qty + unit * (k + 1),
            "pool_after": p_run,
        })
    return rows


def sell_ladder(band_hi: float, model_qty: int, unit: int, steps: int,
                pool: float) -> list[dict]:
    """매도 사다리. steps 단(또는 잔여 소진 전)까지 생성."""
    rows: list[dict] = []
    p_run = pool
    for k in range(steps):
        denom = model_qty - unit * k
        if denom <= 0:
            break
        price = r2(band_hi / denom)
        p_run = r2(p_run + unit * price)
        rows.append({
            "step": k + 1,
            "price": price,
            "qty_model": unit,
            "remain_after": model_qty - unit * (k + 1),
            "pool_after": p_run,
        })
    return rows


def next_cycle_dates(prev_end_yyyymmdd: str) -> tuple[str, str]:
    """다음 주기 (시작, 종료) — 금요일 종료 기준: 시작 = 종료+3일(월), 종료 = 시작+11일(다다음 금)."""
    from datetime import datetime, timedelta
    d = datetime.strptime(prev_end_yyyymmdd, "%Y%m%d")
    start = d + timedelta(days=3)
    end = start + timedelta(days=11)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def build_next_cycle(state: dict, e_value: float) -> dict:
    """현 주기 상태 + 마감 평가금(E) → 다음 주기 제안(전체 사다리 포함).

    state 필수 키: v, pool_now, g, model_qty, unit, buy_limit_pct, sell_steps,
                   mult, cashflow(적립+/인출−), week_no, cyc_end
    """
    v2 = next_v(state["v"], state["pool_now"], state["g"], e_value, state.get("cashflow", 0.0))
    lo, hi = bands(v2)
    pool_next = r2(state["pool_now"] + state.get("cashflow", 0.0))
    buys = buy_ladder(lo, int(state["model_qty"]), int(state["unit"]), pool_next,
                      float(state["buy_limit_pct"]))
    sells = sell_ladder(hi, int(state["model_qty"]), int(state["unit"]),
                        int(state["sell_steps"]), pool_next)
    sta, end = next_cycle_dates(state["cyc_end"])
    mult = int(state.get("mult", 1))
    for r in buys + sells:
        r["qty_acct"] = r["qty_model"] * mult
    return {
        "week_no": int(state["week_no"]) + 2,
        "cyc_start": sta, "cyc_end": end,
        "v": v2, "band_lo": lo, "band_hi": hi,
        "e_used": r2(e_value), "pool_start": pool_next,
        "mult": mult,
        "buys": buys, "sells": sells,
    }
