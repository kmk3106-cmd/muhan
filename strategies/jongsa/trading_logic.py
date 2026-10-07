# -*- coding: utf-8 -*-
"""
종사종팔 v1 - 트렌치 매매 로직
종가 LOC 매수(전일종가×(1+여유%) 한도, '거의 무조건 종가체결'),
**전체평단 +목표% 일괄매도**(보유 전 트렌치 가중평균 평단 기준, 전량 동일 LOC 가격),
N거래일 손절 MOC매도 (N=loss_cut_days, 트렌치별)

떨사오팔(ddsop) 대비 차이:
- 매수: 전일종가×(1+여유%) LOC (사실상 매일 종가 매수; ddsop는 −x% 하락조건부)
- 매도: [2026-06-12 변경] 트렌치별 독립 익절 → **전체 가중평균 평단 ×(1+x%) 에 전량 일괄매도**.
  종가가 목표 이상이면 전 트렌치 동시 체결, 미만이면 전량 미체결(부분매도 없음).
- 손절(40일 MOC, 트렌치별)·트렌치·싸이클(전량매도 → T1 IDLE → 종료)은 ddsop와 동일 골격.
여기서 x_pct 는 '매도 목표 수익률 %'로만 쓰이고 매수 임계엔 사용하지 않는다.

[중요] KIS Open API는 MOC(장마감 시장가)를 '매도 전용'으로만 허용한다(매수 MOC는
APBK1269 거부). 따라서 매수는 반드시 LOC(지정가 장마감)로 내며, 한도가를 전일종가보다
충분히 높게(여유%) 잡아 '무조건 종가매수'에 근접시킨다. 체결가는 한도가가 아니라 실제 종가다.
"""
import logging
import math
from dataclasses import dataclass
from typing import Optional

from .models import Ticker, Tranche, TrancheStatus

logger = logging.getLogger(__name__)

# 종사종팔 매수 LOC 한도가 여유% — 한도가 = 전일종가 × (1 + 이 값/100).
# 클수록 '무조건 종가매수'에 가까움(미체결 확률↓). 종사종팔 전체 공통. (사용자 지정: 15%)
BUY_LIMIT_BUFFER_PCT = 15.0

# 매수 LOC 한도를 '최저 익절가'보다 이만큼(달러) 아래로 캡한다.
# 목적 두 가지 — ① 종사종팔4 "매도 난 날은 매수 금지"를 가격으로 자동 보장
#                (종가는 하나뿐이라 익절가 위면 매도만, 아래면 매수만 체결)
#              ② KIS 자전거래 거부(APBK1943) 회피.
# 좁을수록 좋다. 넓으면 '매도도 매수도 없는 날'(종가가 캡과 익절가 사이)이 생겨
# v4 취지(매도 없었으니 사야 하는 날)를 어긴다.
# [2026-10-07] 기존 0.5% 비율 캡 → 고정 $0.05 로 축소. 0.5% 간격은 KIS 통과가 확인된 값이고
# ($158.60 매도 / $157.81 매수, 2026-08 실체결), 더 좁혀도 되는지는 실거래로 확인하며 줄인다.
BUY_CAP_GAP_USD = 0.05

@dataclass
class OrderItem:
    ticker: str
    tranche_id: int
    tranche_num: int
    cycle_number: int  # 몇 번 싸이클
    side: str          # buy / sell
    order_type: str    # LOC / MOC
    price: float
    qty: int
    desc: str = ""     # 산출근거


def find_next_buy_tranche(tranches: list[Tranche]) -> Optional[Tranche]:
    """
    다음 매수 대상 트렌치 탐색.
    우선순위:
      1) 시퀀스 전진: IDLE이고 직전(K-1)이 BOUGHT인 것 중 가장 높은 번호
         → 손절로 빈 공석은 건너뛰고 다음 번호를 계속 매수
      2) 공석 채우기: 시퀀스 전진이 불가능할 때 가장 낮은 eligible IDLE
         → 모든 후순위 매수가 끝나면 빈 자리를 채움
    """
    sorted_t = sorted(tranches, key=lambda t: t.tranche_num)

    advance = None
    for t in sorted_t:
        if t.status != TrancheStatus.IDLE.value:
            continue
        if t.tranche_num == 1:
            continue
        prev = next((tr for tr in sorted_t if tr.tranche_num == t.tranche_num - 1), None)
        if prev and prev.status == TrancheStatus.BOUGHT.value:
            advance = t

    if advance:
        return advance

    for t in sorted_t:
        if t.status != TrancheStatus.IDLE.value:
            continue
        if t.tranche_num == 1:
            return t
        prev = next((tr for tr in sorted_t if tr.tranche_num == t.tranche_num - 1), None)
        if prev and prev.status == TrancheStatus.BOUGHT.value:
            return t

    return None


def generate_orders(
    ticker_obj: Ticker,
    tranches: list[Tranche],
    prev_close: float,
    today_str: str,
    actual_cash: Optional[float] = None,
) -> list[OrderItem]:
    """
    오늘 제출할 주문 목록 생성. (종사종팔: 매수만 종가 MOC 무조건)
    1) BOUGHT 트렌치 → LOC매도 (평단가 +목표%)  ← 목표가 보장, 종가 목표 이상일 때만 체결
    2) N일 경과 BOUGHT 트렌치 → MOC매도 (손절, N=loss_cut_days)
    3) 다음 매수 대상 → MOC매수 (종가 무조건, 수량=floor(트렌치금액/전일종가))

    씨드반영여부(seed_reflect_enabled):
    - OFF: 싸이클 내 향후 트렌치 매수금액 = total_usd/num_tranches (추가입금 미반영)
    - ON: 잔여 IDLE 트렌치가 있을 때 amt_per = actual_cash/잔여트렌치수
          (모두 BOUGHT이면 추가입금 미반영, 다음 싸이클에서 반영)
    """
    orders = []
    x = ticker_obj.x_pct
    symbol = ticker_obj.ticker
    num_tranches = ticker_obj.num_tranches
    total_usd = ticker_obj.total_usd
    seed_reflect = getattr(ticker_obj, "seed_reflect_enabled", False) or False

    # 매수 1회당 금액: OFF=기존배분, ON=보유현금/잔여트렌치(잔여트렌치 있을 때만)
    remaining_idle = sum(1 for t in tranches if t.status == TrancheStatus.IDLE.value)
    if not seed_reflect or remaining_idle == 0 or actual_cash is None or actual_cash <= 0:
        amt_per = total_usd / num_tranches
    else:
        amt_per = actual_cash / remaining_idle
    # [종사종팔4] 익절 복리 — 지금까지 쌓인 '익절수익 ÷ 트렌치수' 를 1회 매수금액에 더한다.
    amt_per += float(getattr(ticker_obj, "compound_add", 0.0) or 0.0)
    # [종사종팔4] 예수금이 목표금액보다 적으면 있는 예수금만큼만 매수 (원문 e 마지막 줄)
    if actual_cash is not None and 0 < actual_cash < amt_per:
        logger.info(f"[{symbol}] 예수금 ${actual_cash:.2f} < 1회 매수금액 ${amt_per:.2f} "
                    f"→ 예수금만큼만 매수")
        amt_per = actual_cash

    if prev_close <= 0:
        logger.warning(f"[{symbol}] 전일종가 0 - 주문 생성 스킵")
        return orders

    bought_tranches = [t for t in tranches if t.status == TrancheStatus.BOUGHT.value]
    loss_cut_ids = set()
    loss_cut_days = getattr(ticker_obj, "loss_cut_days", 40) or 40

    # ── 손절 (트렌치별 유지): 40거래일 경과 트렌치는 MOC 손절 ──
    for t in bought_tranches:
        cy = getattr(t, "cycle_number", 1) or 1
        if t.days_held >= loss_cut_days:
            orders.append(OrderItem(
                ticker=symbol,
                tranche_id=t.id,
                tranche_num=t.tranche_num,
                cycle_number=cy,
                side="sell",
                order_type="MOC",
                price=0.0,
                qty=t.qty,
                desc=f"손절 (보유 {t.days_held}일 >= {loss_cut_days}일)",
            ))
            loss_cut_ids.add(t.id)

    # ── 익절 매도 [종사종팔4 2026-10: 매수분별 독립매도로 환원] ──
    # 각 트렌치를 **자기 매수가 × (1+목표%)** 에 따로 LOC 매도한다. 트렌치마다 가격이 다르므로
    # 종가에 따라 일부만 체결된다 (원문 c: "매일 매수한 수량 만큼, 매수가+2.7% 에 LOC 매도").
    # (직전 규칙이던 '전체평단 가중평균 일괄매도'는 v4 원문과 달라 폐기 — 2026-06-12 → 2026-10-07)
    sell_targets = [t for t in bought_tranches if t.id not in loss_cut_ids]
    sell_target_price = None            # 매수 한도 캡에 쓸 '최저 익절가'
    for t in sell_targets:
        base_px = t.buy_price or t.avg_price
        if not base_px or t.qty <= 0:
            continue
        tgt = round(base_px * (1 + x / 100), 2)
        sell_target_price = tgt if sell_target_price is None else min(sell_target_price, tgt)
        cy = getattr(t, "cycle_number", 1) or 1
        orders.append(OrderItem(
            ticker=symbol,
            tranche_id=t.id,
            tranche_num=t.tranche_num,
            cycle_number=cy,
            side="sell",
            order_type="LOC",
            price=tgt,
            qty=t.qty,
            desc=f"매수가 ${base_px:.2f} * +{x}% 익절 ({t.qty}주)",
        ))

    # ── 매수: KIS가 MOC 매수를 거부(매도전용, APBK1269)하므로 LOC(지정가 장마감)로 매수.
    # 한도가 = 전일종가 × (1 + 여유%) → 종가가 한도 이하면 종가에 체결('거의 무조건 종가매수').
    # [매수규칙 재조정 2026-06-12] 일괄매도 목표가와 충돌하지 않게 한도를
    # 매도목표가 × 0.995 아래로 캡 → 종가가 목표 위면 전량매도만, 아래면 매수만 체결(자전거래 불가).
    # [종사종팔4] 손절 MOC 가 나가는 날은 **매수 주문을 내지 않는다**.
    # 원문 (1): "매도 발생한 날은 익절/손절 모두 매수 금지".
    # 익절(LOC)은 아래 가격 캡으로 자동 배타되지만(종가는 하나뿐이라 둘 다 체결될 수 없다),
    # MOC 는 가격 조건이 없어 캡이 통하지 않는다. 실제로 KIS 가 자전거래(APBK1943)로 거부했다
    # — 2026-10-02~06 UPRO 매수 24회 연속 거부. 그래서 주문 자체를 생략한다.
    if loss_cut_ids:
        logger.info(f"[{symbol}] 손절 MOC {len(loss_cut_ids)}건 → 오늘 매수 생략 (매도일 매수금지)")
        return orders

    next_t = find_next_buy_tranche(tranches)
    if next_t:
        buy_limit = round(prev_close * (1 + BUY_LIMIT_BUFFER_PCT / 100), 2)
        cap_note = ""
        if sell_target_price is not None:
            cap = round(sell_target_price - BUY_CAP_GAP_USD, 2)
            if buy_limit > cap:
                buy_limit = cap
                cap_note = f", 최저익절가${sell_target_price:.2f} 아래로 캡"
        buy_qty = max(1, int(amt_per / prev_close))
        cy = getattr(ticker_obj, "current_cycle", 1) or 1
        orders.append(OrderItem(
            ticker=symbol,
            tranche_id=next_t.id,
            tranche_num=next_t.tranche_num,
            cycle_number=cy,
            side="buy",
            order_type="LOC",
            price=buy_limit,
            qty=buy_qty,
            desc=f"종가 LOC 매수 (한도 ${buy_limit:.2f}{cap_note})",
        ))

    return orders


def check_cycle_end(tranches: list[Tranche], ticker_obj: Ticker) -> bool:
    """
    1번 트렌치가 매도 완료(IDLE)이고 현재 싸이클에서 매수 이력이 있었으면
    → 싸이클 종료로 판단
    """
    t1 = next((t for t in tranches if t.tranche_num == 1), None)
    if t1 is None:
        return False
    return t1.status == TrancheStatus.IDLE.value and t1.cycle_number == ticker_obj.current_cycle
