# -*- coding: utf-8 -*-
"""
무한매수법 - 트레이딩 로직 (버전별 디스패치)
T, ☆% 계산, 주문 세트 생성, API 잔고/체결 → state 동기화

지원 버전: 2.2 (기본), 3.0 (추가 예정)
"""
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import List

from .models import ModeEnum, Portfolio, PortfolioState

logger = logging.getLogger(__name__)

# 지원하는 전략 버전 목록 (추가 시 여기 등록)
SUPPORTED_VERSIONS = ["2.2", "3.0"]


# ========== 기본 계산 함수 ==========
def round_up(value: float, decimals: int = 2) -> float:
    """소수 N자리 반올림 (앱과 동일한 표시용)"""
    d = Decimal(str(value))
    quantize = Decimal(10) ** -decimals
    return float(d.quantize(quantize, rounding=ROUND_HALF_UP))


def calc_T(cum_buy: float, B: float, cum_sell: float = 0.0) -> float:
    """
    T(현재회차) = (매수누적액 - 매도누적액) / 1회매수액
    매도가 체결되면 T가 줄어듦.
    소수 둘째자리에서 올림하여 소수 첫째자리까지 표기 (반올림 아님).
    """
    if B <= 0:
        return 0.0
    net = max(cum_buy - cum_sell, 0.0)
    raw = net / B
    return round(math.ceil(raw * 10) / 10, 1)


def calc_T_from_avg(avg_price: float, qty: int, B: float) -> float:
    """
    T(현재회차) = 잔고매입금액 / 1회매수액

    잔고매입금액은 평균단가×보유수량(증권사 잔고 기준)으로 계산한다.
    매도 체결 이후에도 '현재 남아있는 보유 원가'를 기준으로 T를 계산하고 싶을 때 사용.
    """
    if B <= 0:
        return 0.0
    holding_cost = max(float(avg_price or 0) * max(int(qty or 0), 0), 0.0)
    raw = holding_cost / B
    return round(math.ceil(raw * 10) / 10, 1)


def calc_star_pct(T: float, A: int = 40, R: float = 10.0) -> float:
    """
    ☆% = R - (T/2) × (40/A)
    R = 목표수익률(%). A!=40일 때 보정
    """
    if A <= 0:
        return R
    return R - (T / 2) * (40.0 / A)


# ========== 주문 아이템 ==========
@dataclass
class OrderItem:
    """주문 1건 (API 제출 전 내부 표현)"""
    side: str          # buy / sell
    order_type: str    # LOC / MOC / LIMIT
    ord_dvsn: str      # 00/33/34 (한국투자 API 코드)
    price: float
    qty: int
    amount: float = 0.0  # 매수 시 금액 (B/2, B 등)


# ========== V2.2 주문 생성 ==========
def _generate_orders_v22(
    portfolio: Portfolio,
    state: PortfolioState,
    today: str,
) -> List[OrderItem]:
    """
    무한매수법 V2.2 규칙
    NORMAL 전반(T<20): 매수 2개(B/2 @ AVG, B/2 @ ☆%) + 매도 2개(1/4 LOC, 3/4 +10%)
    NORMAL 후반(T>=20): 매수 1개(B @ ☆%) + 매도 2개
    QUARTER(1~10): 매수 1개(-10% LOC) + 매도 2개(-10% LOC, +10% 지정가)
    QUARTER(10회 직후): MOC 1/4 매도
    """
    orders: List[OrderItem] = []
    B = portfolio.B
    A = portfolio.A
    avg = state.avg_price
    qty = state.qty
    star_pct = state.star_pct / 100.0
    mode = state.mode
    quarter_step = state.quarter_step

    def buy_price(base_price: float) -> float:
        return round(base_price - 0.01, 2)

    def sell_price(base_price: float) -> float:
        return round(base_price, 2)

    if avg <= 0 or B <= 0:
        logger.debug(f"avg={avg}, B={B} - 주문 생성 스킵 (최초매수 전)")
        return orders

    if mode == ModeEnum.QUARTER.value:
        target_r = getattr(portfolio, "R", 10.0) or 10.0
        loc_minus_r = avg * (1 - target_r / 100.0)
        limit_plus_r = avg * (1 + target_r / 100.0)

        if quarter_step == 0 or quarter_step > 10:
            # step 0: 진입 직후 또는 10회 완료 후 → MOC 1/4 매도만 (매수 없음)
            sell_qty = max(1, int(qty * 0.25))
            if sell_qty > 0 and qty > 0:
                orders.append(OrderItem(
                    side="sell", order_type="MOC", ord_dvsn="33",
                    price=0.0, qty=sell_qty,
                ))
        else:
            # step 1~10: LOC 매수 + LOC 매도 1/4 + LIMIT 매도 3/4
            Bq = min(B, state.quarter_base_cash) if state.quarter_base_cash > 0 else B
            if Bq > 0 and loc_minus_r > 0:
                buy_qty = max(1, int(Bq / buy_price(loc_minus_r)))
                if buy_qty > 0:
                    orders.append(OrderItem(
                        side="buy", order_type="LOC", ord_dvsn="34",
                        price=buy_price(loc_minus_r), qty=buy_qty, amount=Bq,
                    ))
            sell1_qty = max(1, int(qty * 0.25))
            sell2_qty = qty - sell1_qty
            if sell1_qty > 0 and qty > 0:
                orders.append(OrderItem(
                    side="sell", order_type="LOC", ord_dvsn="34",
                    price=sell_price(loc_minus_r), qty=sell1_qty,
                ))
            if sell2_qty > 0:
                orders.append(OrderItem(
                    side="sell", order_type="LIMIT", ord_dvsn="00",
                    price=sell_price(limit_plus_r), qty=sell2_qty,
                ))
    else:
        if state.T < 20:
            half_B = B / 2
            buy1_qty = max(1, int(half_B / buy_price(avg)))
            buy2_qty = max(1, int(half_B / buy_price(avg * (1 + star_pct))))
            if buy1_qty > 0:
                orders.append(OrderItem(
                    side="buy", order_type="LOC", ord_dvsn="34",
                    price=buy_price(avg), qty=buy1_qty, amount=half_B,
                ))
            if buy2_qty > 0:
                orders.append(OrderItem(
                    side="buy", order_type="LOC", ord_dvsn="34",
                    price=buy_price(avg * (1 + star_pct)), qty=buy2_qty, amount=half_B,
                ))
        else:
            buy_qty = max(1, int(B / buy_price(avg * (1 + star_pct))))
            if buy_qty > 0:
                orders.append(OrderItem(
                    side="buy", order_type="LOC", ord_dvsn="34",
                    price=buy_price(avg * (1 + star_pct)), qty=buy_qty, amount=B,
                ))

        target_r = getattr(portfolio, "R", 10.0) or 10.0
        loc_price = avg * (1 + star_pct)
        limit_price = avg * (1 + target_r / 100.0)
        sell1_qty = int(qty * 0.25)
        sell2_qty = qty - sell1_qty
        if sell1_qty > 0:
            orders.append(OrderItem(
                side="sell", order_type="LOC", ord_dvsn="34",
                price=sell_price(loc_price), qty=sell1_qty,
            ))
        if sell2_qty > 0:
            orders.append(OrderItem(
                side="sell", order_type="LIMIT", ord_dvsn="00",
                price=sell_price(limit_price), qty=sell2_qty,
            ))

    return orders


# ========== V3.0 주문 생성 (추가 예정) ==========
def _generate_orders_v30(
    portfolio: Portfolio,
    state: PortfolioState,
    today: str,
) -> List[OrderItem]:
    """
    무한매수법 V3.0 규칙 (추가 예정)
    TODO: V3.0 로직 구현 시 이 함수에 작성
    """
    raise NotImplementedError("무한매수법 V3.0은 아직 구현되지 않았습니다. V2.2를 사용하세요.")


# ========== 버전별 디스패치 ==========
_ORDER_GENERATORS = {
    "2.2": _generate_orders_v22,
    "3.0": _generate_orders_v30,
}


def generate_orders(
    portfolio: Portfolio,
    state: PortfolioState,
    today: str,
) -> List[OrderItem]:
    """
    포트폴리오의 strategy_version에 따라 해당 버전 주문 생성 로직 호출
    """
    version = getattr(portfolio, "strategy_version", "2.2") or "2.2"
    gen = _ORDER_GENERATORS.get(version)
    if gen is None:
        logger.warning(f"미지원 버전 {version}, V2.2로 폴백")
        gen = _generate_orders_v22
    return gen(portfolio, state, today)


# ========== 상태 동기화 ==========
def sync_state_from_api(
    portfolio: Portfolio,
    state: PortfolioState,
    balance_df1,
    balance_df2,
    ccnl_df,
    *,
    trade_cutoff_date: str | None = None,
) -> PortfolioState:
    """
    API 잔고/체결 데이터로 state 갱신
    (기본 로직은 V2.2 기준. V3.0에서 다르면 별도 함수 추가)
    """
    import pandas as pd
    ticker = portfolio.ticker

    # df1 = output1 (종목별 잔고), df2 = output2 (계좌 요약)
    # df1에서 해당 ticker를 찾아 평단가/수량 추출
    rows = pd.DataFrame()
    for df in (balance_df1, balance_df2):
        if df.empty:
            continue
        for col in ("ovrs_pdno", "pdno", "OVRS_PDNO"):
            if col in df.columns:
                match = df[df[col].astype(str).str.strip() == str(ticker).strip()]
                if not match.empty:
                    rows = match
                    break
        if not rows.empty:
            break

    balance_buy_amt = 0.0  # 잔고 API 종목별 매입금액 (frcr_pchs_amt1) - T 계산의 신뢰할 수 있는 기준
    # 잔고에 해당 종목이 없으면 전량 매도 등으로 보유 0 → DB에 남은 qty/avg를 반드시 초기화
    if rows.empty:
        state.qty = 0
        state.avg_price = 0.0
    else:
        row = rows.iloc[0]
        state.qty = 0
        state.avg_price = 0.0
        for qty_col in ("ovrs_cblc_qty", "ord_psbl_qty", "ovrs_stck_evlu_qty", "nrcvb_qty"):
            if qty_col in row and row[qty_col] is not None and str(row[qty_col]).strip() != "":
                try:
                    state.qty = max(0, int(float(row[qty_col])))
                except (TypeError, ValueError):
                    state.qty = 0
                break
        for avg_col in ("pchs_avg_pric", "ovrs_stck_avg_pric", "PCHS_AVG_PRIC"):
            if avg_col in row and row[avg_col]:
                try:
                    state.avg_price = float(row[avg_col])
                except (TypeError, ValueError):
                    state.avg_price = 0.0
                break
        if state.qty <= 0:
            state.avg_price = 0.0
        for buy_col in ("frcr_pchs_amt1", "FRCR_PCHS_AMT1"):
            if buy_col in row and row[buy_col] is not None:
                v = float(row[buy_col])
                if v > 0:
                    balance_buy_amt = v
                    break

    for df in (balance_df1, balance_df2):
        if not df.empty:
            row1 = df.iloc[0]
            for cash_col in ("frcr_evlu_psbl_amt", "frcr_pchs_psbl_amt", "tot_evlu_pfls_amt"):
                if cash_col in row1 and row1[cash_col]:
                    v = float(row1[cash_col])
                    if v > 0:
                        state.cash = v
                        break
            if state.cash > 0:
                break

    if not ccnl_df.empty:
        ticker_df = pd.DataFrame()
        for col in ("pdno", "PDNO", "ovrs_pdno", "OVRS_PDNO"):
            if col in ccnl_df.columns:
                match = ccnl_df[ccnl_df[col].astype(str).str.strip() == ticker]
                if not match.empty:
                    ticker_df = match
                    break
        if not ticker_df.empty:
            # 체결 합산 하한: 워커에서 전달 시 이전 싸이클 종료 **다음날**부터만 (싸이클별 누적)
            if trade_cutoff_date:
                cutoff_date = str(trade_cutoff_date).strip()
            else:
                cycle_start = getattr(portfolio, "cycle_start_date", None) or None
                pf_created = getattr(portfolio, "created_at", None)
                pf_created_str = pf_created.strftime("%Y%m%d") if pf_created else None
                if cycle_start and pf_created_str:
                    cutoff_date = min(cycle_start, pf_created_str)
                else:
                    cutoff_date = cycle_start or pf_created_str
            buy_cum, sell_cum = 0.0, 0.0
            for _, row in ticker_df.iterrows():
                if cutoff_date:
                    trade_date_raw = str(row.get("ord_dt", "") or "")
                    if trade_date_raw and trade_date_raw < cutoff_date:
                        continue
                side_code = str(row.get("sll_buy_dvsn_cd", "") or "")
                price = float(row.get("ccld_unpr", 0) or row.get("ft_ccld_unpr3", 0) or 0)
                qty = int(float(row.get("ft_ccld_qty", 0) or row.get("ccld_qty", 0) or 0))
                tot_amt = float(row.get("tot_ccld_amt", 0) or 0)
                fill_amt = round(tot_amt, 2) if tot_amt > 0 else (round(price * qty, 2) if price > 0 and qty > 0 else 0.0)
                if side_code in ("02", "2"):
                    buy_cum += fill_amt
                elif side_code in ("01", "1"):
                    sell_cum += fill_amt
            init_cost = getattr(portfolio, "initial_holdings_cost", 0) or 0
            state.cum_buy_amount = round(init_cost + buy_cum, 2)
            if sell_cum > 0:
                state.cum_sell_amount = round(sell_cum, 2)

    # T = (총 매수 누적액 - 총 매도 누적액) / B (체결 합산이 정확함)
    # frcr_pchs_amt1=현재 보유분 매입금액=평단×수량 → 매도 후에는 (cum_buy-cum_sell)과 다름. 사용 금지.
    if state.cum_sell_amount > 0:
        # 매도 있음: frcr_pchs_amt1(평단×수량) ≠ net. ccnl 체결 합산만 사용
        pass
    elif balance_buy_amt > 0 and state.qty > 0 and portfolio.B > 0 and state.cum_buy_amount <= 0:
        # ccnl 없음 + 매도 이력 없음: 잔고 매입금액 = cum_buy 와 동일
        state.cum_buy_amount = round(balance_buy_amt, 2)
        state.cum_sell_amount = 0.0
        logger.debug(f"[{ticker}] T 계산: ccnl 없음, 잔고 매입금액 ${balance_buy_amt:.2f} fallback")
    else:
        # ccnl 누락 시 하한 보정 (cum_buy < 보유분일 때)
        if state.avg_price > 0 and state.qty > 0:
            estimated_buy = round(state.avg_price * state.qty, 2)
            if state.cum_buy_amount < estimated_buy:
                logger.info(f"[{ticker}] cum_buy_amount ${state.cum_buy_amount:.2f} < "
                            f"보유분 ${state.avg_price:.2f}×{state.qty}=${estimated_buy:.2f} → 보정")
                state.cum_buy_amount = estimated_buy
        # cum_buy 비정상적으로 클 때 상한 보정
        if state.avg_price > 0 and state.qty > 0 and portfolio.B > 0:
            max_reasonable = round(state.avg_price * state.qty * 1.5, 2)
            if state.cum_buy_amount > max_reasonable and state.cum_sell_amount == 0:
                logger.warning(f"[{ticker}] cum_buy ${state.cum_buy_amount:.2f} 비정상 → 보정")
                state.cum_buy_amount = round(state.avg_price * state.qty, 2)
                state.cum_sell_amount = 0.0

    # T는 현금흐름(순투입) 대신 잔고 원가(평단×수량) 기준으로 계산한다.
    state.T = calc_T_from_avg(state.avg_price, state.qty, portfolio.B)
    target_r = getattr(portfolio, "R", 10.0) or 10.0
    state.star_pct = calc_star_pct(state.T, portfolio.A, target_r)

    # V2.2 모드 전환
    version = getattr(portfolio, "strategy_version", "2.2") or "2.2"
    if version == "2.2":
        if state.mode != ModeEnum.QUARTER.value and 39.1 <= state.T <= 40:
            # NORMAL → QUARTER 진입: 원금 소진 시점
            state.mode = ModeEnum.QUARTER.value
            state.quarter_step = 0  # step 0 = MOC 1/4 매도로 시작
            state.quarter_base_cash = 0.0
            logger.info(f"[{ticker}] QUARTER 모드 진입 (T={state.T})")

    return state


# ========== 계좌 요약 추출 ==========
def extract_account_summary(balance_df1, balance_df2) -> dict:
    """
    KIS 해외주식 잔고 API (TTTS3012R) 응답에서 계좌 정보 추출

    output1 (balance_df1) - 종목별:
      ovrs_stck_evlu_amt: 해외주식평가금액
      frcr_evlu_pfls_amt: 외화평가손익금액
      evlu_pfls_rt: 평가손익율
      frcr_pchs_amt1: 외화매입금액1

    output2 (balance_df2) - 계좌 요약:
      frcr_pchs_amt1: 외화매입금액1 (총매입)
      ovrs_tot_pfls: 해외총손익
      tot_evlu_pfls_amt: 총평가손익금액
      tot_pftrt: 총수익률
      frcr_buy_amt_smtl1: 외화매수금액합계1

    반환: {"stock_evlu", "buy_amt", "pnl", "pnl_rt", "tot_pfls"}
    """
    stock_evlu = 0.0  # 주식평가액 합계
    buy_amt = 0.0     # 총매입금액
    pnl = 0.0         # 평가손익
    pnl_rt = 0.0      # 평가손익율
    tot_pfls = 0.0    # 해외총손익 (실현+평가)

    def _float(val):
        try:
            return float(val) if val is not None and str(val).strip() else 0.0
        except (TypeError, ValueError):
            return 0.0

    # output1: 종목별 합산
    if not balance_df1.empty:
        if "ovrs_stck_evlu_amt" in balance_df1.columns:
            stock_evlu = balance_df1["ovrs_stck_evlu_amt"].apply(_float).sum()
        if "frcr_evlu_pfls_amt" in balance_df1.columns:
            pnl = balance_df1["frcr_evlu_pfls_amt"].apply(_float).sum()
        if "frcr_pchs_amt1" in balance_df1.columns:
            buy_amt = balance_df1["frcr_pchs_amt1"].apply(_float).sum()

    # output2: 계좌 요약 (output1에서 못 구한 값 보완)
    if not balance_df2.empty:
        row = balance_df2.iloc[0]
        if pnl == 0:
            pnl = _float(row.get("tot_evlu_pfls_amt", 0))
        if buy_amt <= 0:
            buy_amt = _float(row.get("frcr_pchs_amt1", 0))
            if buy_amt <= 0:
                buy_amt = _float(row.get("frcr_buy_amt_smtl1", 0))
        tot_pfls = _float(row.get("ovrs_tot_pfls", 0))
        pnl_rt = _float(row.get("tot_pftrt", 0))

    # 평가손익율 직접 계산 (API 값이 없으면)
    if pnl_rt == 0 and buy_amt > 0 and pnl != 0:
        pnl_rt = round(pnl / buy_amt * 100, 2)

    # 주식평가액이 0이면 매입금액+손익으로 추정
    if stock_evlu <= 0 and buy_amt > 0:
        stock_evlu = buy_amt + pnl

    return {
        "stock_evlu": round(stock_evlu, 2),
        "buy_amt": round(buy_amt, 2),
        "pnl": round(pnl, 2),
        "pnl_rt": round(pnl_rt, 2),
        "tot_pfls": round(tot_pfls, 2),
    }


# ========== 중복 방지 ==========
def orders_hash(orders: List[OrderItem], ticker: str, today: str) -> str:
    """주문 세트 해시 (중복 제출 방지)"""
    data = {"ticker": ticker, "date": today, "orders": []}
    for o in orders:
        data["orders"].append({
            "side": o.side, "type": o.order_type, "price": o.price, "qty": o.qty,
        })
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
