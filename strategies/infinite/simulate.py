# -*- coding: utf-8 -*-
"""
무한매수법 V2.2 로컬 시뮬레이터

실제 KIS API 없이 가상 주가로 전체 매매 흐름을 테스트합니다.
- 주문 생성: trading_logic.py의 generate_orders 그대로 사용
- 체결 판단: 가상 주가 vs 주문가격 비교
- DB 기록: trades, portfolio_states 실제 DB에 적재
- 대시보드에서 결과 확인 가능

DB: infinite_buy_sim.db (실제 거래 DB와 완전 분리)

사용법:
  python simulate.py --reset             # 시뮬 데이터 초기화 후 기본 시나리오 실행
  python simulate.py --days 60           # 60일 시뮬레이션
  python simulate.py --scenario crash    # 폭락 시나리오 (QUARTER 테스트)
  python simulate.py --ticker SOXL       # SOXL 티커로 시뮬레이션

대시보드에서 시뮬 결과 확인:
  set DATABASE_URL=sqlite:///infinite_buy_sim.db
  python main.py
"""
import argparse
import logging
import math
import random
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .config import BASE_DIR
from .models import (init_db, Portfolio, PortfolioState, Trade, Order,
                    CycleHistory, ModeEnum)
from .trading_logic import (generate_orders, calc_T_from_avg, calc_star_pct,
                           OrderItem, orders_hash)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("simulator")

SIM_DATABASE_URL = f"sqlite:///{BASE_DIR / 'infinite_buy_sim.db'}"
engine = init_db(SIM_DATABASE_URL)


# ========== 가상 주가 생성 ==========
def generate_prices(start_price: float, days: int, scenario: str = "default") -> list:
    """
    날짜별 가상 주가를 생성합니다.
    반환: [(day_index, open, high, low, close), ...]
    """
    prices = []
    price = start_price

    if scenario == "default":
        # 기본 시나리오: 하락 → 횡보 → 반등 → 급등(익절)
        for day in range(days):
            pct = day / days
            if pct < 0.3:
                # 초반 30%: 하락 (-0.5% ~ -2% 일일 변동)
                drift = random.uniform(-0.02, 0.005)
            elif pct < 0.6:
                # 중반 30%: 횡보 (-1% ~ +1%)
                drift = random.uniform(-0.01, 0.01)
            elif pct < 0.85:
                # 후반 25%: 서서히 반등 (0% ~ +2%)
                drift = random.uniform(0.0, 0.02)
            else:
                # 마지막 15%: 급등 (+1% ~ +3%)
                drift = random.uniform(0.01, 0.03)

            noise = random.uniform(-0.005, 0.005)
            price = price * (1 + drift + noise)
            price = max(price * 0.5, price)  # 바닥 방어

            open_p = price * random.uniform(0.99, 1.01)
            high_p = price * random.uniform(1.0, 1.03)
            low_p = price * random.uniform(0.97, 1.0)
            close_p = price

            prices.append({
                "day": day + 1,
                "open": round(open_p, 2),
                "high": round(high_p, 2),
                "low": round(low_p, 2),
                "close": round(close_p, 2),
            })

    elif scenario == "crash":
        # 폭락 → 회복 시나리오: 전반 폭락(QUARTER 진입) + 후반 회복(후반전 복귀)
        crash_days = days
        recovery_days = 40
        total = crash_days + recovery_days
        for day in range(total):
            if day < crash_days:
                drift = random.uniform(-0.03, 0.01)
            else:
                # 회복 구간: 점진적 상승
                pct = (day - crash_days) / recovery_days
                if pct < 0.3:
                    drift = random.uniform(0.0, 0.02)
                elif pct < 0.7:
                    drift = random.uniform(0.01, 0.03)
                else:
                    drift = random.uniform(0.02, 0.04)
            price = price * (1 + drift)
            prices.append({
                "day": day + 1,
                "open": round(price * 1.01, 2),
                "high": round(price * 1.02, 2),
                "low": round(price * 0.98, 2),
                "close": round(price, 2),
            })

    elif scenario == "recovery":
        # V자 반등: 급락 후 빠른 회복
        for day in range(days):
            pct = day / days
            if pct < 0.4:
                drift = random.uniform(-0.03, 0.005)
            else:
                drift = random.uniform(0.005, 0.04)
            price = price * (1 + drift)
            prices.append({
                "day": day + 1,
                "open": round(price * 1.005, 2),
                "high": round(price * 1.02, 2),
                "low": round(price * 0.98, 2),
                "close": round(price, 2),
            })

    return prices


# ========== 체결 판단 ==========
def check_execution(order: OrderItem, price_data: dict) -> dict | None:
    """
    주문이 체결되었는지 가상 주가 기준으로 판단합니다.
    반환: {"price": 체결가, "qty": 체결수량} 또는 None (미체결)
    """
    close = price_data["close"]
    low = price_data["low"]
    high = price_data["high"]

    if order.order_type == "MOC":
        # MOC: 종가로 무조건 체결
        return {"price": close, "qty": order.qty}

    elif order.order_type == "LOC":
        if order.side == "buy":
            # LOC 매수: 종가가 주문가 이하면 체결 (종가로)
            if close <= order.price:
                return {"price": close, "qty": order.qty}
        else:
            # LOC 매도: 종가가 주문가 이상이면 체결 (종가로)
            if close >= order.price:
                return {"price": close, "qty": order.qty}

    elif order.order_type == "LIMIT":
        if order.side == "buy":
            # 지정가 매수: 장중 저가가 주문가 이하면 체결
            if low <= order.price:
                return {"price": order.price, "qty": order.qty}
        else:
            # 지정가 매도: 장중 고가가 주문가 이상이면 체결
            if high >= order.price:
                return {"price": order.price, "qty": order.qty}

    return None


# ========== 상태 업데이트 ==========
def update_state_after_execution(state: PortfolioState, portfolio: Portfolio,
                                 exec_result: dict, side: str):
    """체결 후 상태를 업데이트합니다."""
    price = exec_result["price"]
    qty = exec_result["qty"]
    amount = round(price * qty, 2)

    if side == "buy":
        total_cost = state.avg_price * state.qty + amount
        state.qty += qty
        state.avg_price = round(total_cost / state.qty, 4) if state.qty > 0 else 0.0
        state.cum_buy_amount += amount
    else:
        state.qty = max(0, state.qty - qty)
        state.cum_sell_amount += amount
        if state.qty == 0:
            state.avg_price = 0.0

    state.T = calc_T_from_avg(state.avg_price, state.qty, portfolio.B)
    target_r = getattr(portfolio, "R", 10.0) or 10.0
    state.star_pct = calc_star_pct(state.T, portfolio.A, target_r)


# ========== 싸이클 종료 체크 ==========
def check_cycle_end(session: Session, portfolio: Portfolio,
                    state: PortfolioState, today: str) -> bool:
    """싸이클 종료 감지 및 이력 기록"""
    if not getattr(portfolio, "initial_buy_done", False):
        return False
    if state.qty > 0 or state.cum_buy_amount <= 0:
        return False

    from sqlalchemy import func
    cycle_num = getattr(portfolio, "current_cycle", 1) or 1
    start_date = getattr(portfolio, "cycle_start_date", None) or today

    buy_sum = session.scalar(
        select(func.coalesce(func.sum(Trade.amount), 0.0))
        .where(Trade.portfolio_id == portfolio.id,
               Trade.side == "buy",
               Trade.trade_date >= start_date)
    ) or 0.0
    sell_sum = session.scalar(
        select(func.coalesce(func.sum(Trade.amount), 0.0))
        .where(Trade.portfolio_id == portfolio.id,
               Trade.side == "sell",
               Trade.trade_date >= start_date)
    ) or 0.0

    init_cost = float(getattr(portfolio, "initial_holdings_cost", 0) or 0)
    buy_total = buy_sum + (init_cost if cycle_num == 1 and init_cost > 0 else 0.0)

    profit = sell_sum - buy_total
    profit_pct = (profit / buy_total * 100) if buy_total > 0 else 0.0

    cycle = CycleHistory(
        portfolio_id=portfolio.id,
        cycle_number=cycle_num,
        start_date=start_date,
        end_date=today,
        total_buy_amount=round(buy_total, 2),
        total_sell_amount=round(sell_sum, 2),
        profit=round(profit, 2),
        profit_pct=round(profit_pct, 2),
    )
    session.add(cycle)

    logger.info(f"{'='*60}")
    logger.info(f"  싸이클 #{cycle_num} 종료!")
    logger.info(f"  총매수: ${buy_total:,.2f} | 총매도: ${sell_sum:,.2f}")
    logger.info(f"  수익: ${profit:,.2f} ({profit_pct:+.2f}%)")
    logger.info(f"{'='*60}")

    state.avg_price = 0.0
    state.qty = 0
    state.cum_buy_amount = 0.0
    state.cum_sell_amount = 0.0
    state.T = 0.0
    state.star_pct = getattr(portfolio, "R", 10.0) or 10.0
    state.mode = "NORMAL"
    state.quarter_step = 0
    state.quarter_base_cash = 0.0

    portfolio.current_cycle = cycle_num + 1
    portfolio.cycle_start_date = None
    portfolio.initial_buy_done = False

    session.commit()
    return True


# ========== QUARTER LOC매도 감지 ==========
def check_quarter_loc_sell(session: Session, portfolio: Portfolio,
                           state: PortfolioState, today_str: str,
                           today_executions: list) -> bool:
    """QUARTER step 1~10 중 LOC 매도가 체결되었으면 NORMAL 복귀"""
    if state.mode != "QUARTER" or not (1 <= state.quarter_step <= 10):
        return False
    for ex in today_executions:
        if ex["side"] == "sell" and ex["order_type"] == "LOC":
            logger.info(f"  >> LOC 매도 체결 감지! QUARTER → NORMAL(후반전) 복귀")
            state.mode = "NORMAL"
            state.quarter_step = 0
            state.quarter_base_cash = 0.0
            return True
    return False


# ========== 메인 시뮬레이션 ==========
def run_simulation(
    ticker: str = "SIM_TEST",
    seed: float = 4000.0,
    A: int = 40,
    R: float = 10.0,
    start_price: float = 50.0,
    days: int = 50,
    scenario: str = "default",
    reset: bool = False,
):
    """시뮬레이션 실행"""
    with Session(engine) as session:
        # 기존 시뮬 데이터 초기화
        if reset:
            logger.info("기존 시뮬레이션 데이터 초기화 중...")
            existing = session.scalar(
                select(Portfolio).where(Portfolio.ticker == ticker)
            )
            if existing:
                session.execute(
                    Trade.__table__.delete().where(Trade.portfolio_id == existing.id))
                session.execute(
                    Order.__table__.delete().where(Order.portfolio_id == existing.id))
                session.execute(
                    CycleHistory.__table__.delete().where(CycleHistory.portfolio_id == existing.id))
                session.execute(
                    PortfolioState.__table__.delete().where(PortfolioState.portfolio_id == existing.id))
                session.delete(existing)
                session.commit()
            logger.info("초기화 완료")

        # 포트폴리오 생성
        pf = session.scalar(select(Portfolio).where(Portfolio.ticker == ticker))
        if not pf:
            pf = Portfolio(
                ticker=ticker,
                strategy_version="2.2",
                seed=seed,
                A=A,
                R=R,
                fee_rate=0.0,
                ovrs_excg_cd="NASD",
                trading_enabled=True,
            )
            session.add(pf)
            session.commit()
            session.refresh(pf)

        # 상태 생성/조회
        state = session.scalar(
            select(PortfolioState)
            .where(PortfolioState.portfolio_id == pf.id)
            .order_by(PortfolioState.id.desc())
            .limit(1)
        )
        if not state:
            state = PortfolioState(portfolio_id=pf.id)
            session.add(state)
            session.commit()

        B = pf.B
        prices = generate_prices(start_price, days, scenario)
        base_date = datetime(2026, 1, 5)

        logger.info(f"{'='*60}")
        logger.info(f"  무한매수법 V2.2 시뮬레이션 시작")
        logger.info(f"  티커: {ticker} | 시드: ${seed:,.0f} | A: {A} | R: {R}%")
        logger.info(f"  B(1회매수금): ${B:,.2f} | 시작가: ${start_price}")
        logger.info(f"  기간: {days}일 | 시나리오: {scenario}")
        logger.info(f"{'='*60}")

        # ===== Day 0: 최초 매수 =====
        day0_price = prices[0]["close"]
        initial_qty = max(1, int(B / day0_price))
        initial_amount = round(day0_price * initial_qty, 2)
        today_str = base_date.strftime("%Y%m%d")

        state.avg_price = day0_price
        state.qty = initial_qty
        state.cum_buy_amount = initial_amount
        state.cum_sell_amount = 0.0
        state.T = calc_T_from_avg(state.avg_price, state.qty, B)
        state.star_pct = calc_star_pct(state.T, A, R)
        state.mode = "NORMAL"

        pf.initial_buy_done = True
        pf.cycle_start_date = today_str
        pf.current_cycle = getattr(pf, "current_cycle", 1) or 1

        # 최초매수 기록
        trade = Trade(
            portfolio_id=pf.id,
            trade_date=today_str,
            side="buy",
            order_type="LIMIT",
            price=day0_price,
            qty=initial_qty,
            amount=initial_amount,
        )
        session.add(trade)
        session.commit()

        logger.info(f"\nDay 0 ({today_str}) | 주가: ${day0_price:.2f}")
        logger.info(f"  [최초매수] ${day0_price:.2f} × {initial_qty}주 = ${initial_amount:.2f}")
        logger.info(f"  → T={state.T}, ☆%={state.star_pct:.1f}%, avg=${state.avg_price:.2f}, qty={state.qty}")

        # ===== Day 1 ~ N: 매일 주문 → 체결 루프 =====
        for price_data in prices[1:]:
            day = price_data["day"]
            current_date = base_date + timedelta(days=day)
            today_str = current_date.strftime("%Y%m%d")
            close = price_data["close"]

            if state.qty <= 0 and state.cum_buy_amount <= 0:
                # 싸이클 종료 후 다음 싸이클 최초매수
                state.avg_price = close
                state.qty = max(1, int(B / close))
                initial_amt = round(close * state.qty, 2)
                state.cum_buy_amount = initial_amt
                state.cum_sell_amount = 0.0
                state.T = calc_T_from_avg(state.avg_price, state.qty, B)
                state.star_pct = calc_star_pct(state.T, A, R)
                pf.initial_buy_done = True
                pf.cycle_start_date = today_str
                trade = Trade(
                    portfolio_id=pf.id, trade_date=today_str, side="buy",
                    order_type="LIMIT", price=close, qty=state.qty,
                    amount=initial_amt,
                )
                session.add(trade)
                session.commit()
                logger.info(f"\nDay {day} ({today_str}) | 주가: ${close:.2f}")
                logger.info(f"  [새 싸이클 최초매수] ${close:.2f} × {state.qty}주")
                continue

            # 주문 생성
            orders = generate_orders(pf, state, today_str)
            if not orders:
                continue

            mode_str = state.mode
            if mode_str == "NORMAL":
                mode_str = "전반전" if state.T < 20 else "후반전"
            elif mode_str == "QUARTER":
                mode_str = f"QUARTER(step={state.quarter_step})"

            logger.info(f"\nDay {day} ({today_str}) | 주가: ${close:.2f} (L:${price_data['low']:.2f} H:${price_data['high']:.2f}) | {mode_str}")

            today_executions = []
            for order in orders:
                exec_result = check_execution(order, price_data)
                order_status = "체결" if exec_result else "미체결"
                marker = "●" if exec_result else "○"

                if exec_result:
                    update_state_after_execution(state, pf, exec_result, order.side)
                    trade = Trade(
                        portfolio_id=pf.id,
                        trade_date=today_str,
                        side=order.side,
                        order_type=order.order_type,
                        price=exec_result["price"],
                        qty=exec_result["qty"],
                        amount=round(exec_result["price"] * exec_result["qty"], 2),
                    )
                    session.add(trade)
                    today_executions.append({
                        "side": order.side,
                        "order_type": order.order_type,
                    })

                side_str = "매수" if order.side == "buy" else "매도"
                exec_info = ""
                if exec_result:
                    exec_info = f" (체결가 ${exec_result['price']:.2f})"
                logger.info(f"  {marker} {side_str} {order.order_type} "
                           f"${order.price:.2f} × {order.qty}주 → {order_status}{exec_info}")

            # QUARTER → NORMAL 전환 감지
            check_quarter_loc_sell(session, pf, state, today_str, today_executions)

            # QUARTER step 관리
            if state.mode == "QUARTER" and today_executions:
                if state.quarter_step == 0:
                    state.quarter_step = 1
                    state.quarter_base_cash = min(B, (state.cum_sell_amount * 0.3) / 10)
                    if state.quarter_base_cash <= 0:
                        state.quarter_base_cash = B * 0.5
                    logger.info(f"  >> QUARTER: MOC 매도 완료, 10회 분할매수금=${state.quarter_base_cash:.2f}, step→1")
                elif 1 <= state.quarter_step <= 10:
                    state.quarter_step += 1
                    if state.quarter_step > 10:
                        state.quarter_step = 0
                        logger.info(f"  >> QUARTER: 10회 완료 → MOC 1/4 매도 예정")

            # QUARTER 진입 체크
            version = getattr(pf, "strategy_version", "2.2") or "2.2"
            if version == "2.2" and state.mode != "QUARTER" and 39.1 <= state.T <= 40:
                state.mode = "QUARTER"
                state.quarter_step = 0
                state.quarter_base_cash = 0.0
                logger.info(f"  >> QUARTER 모드 진입! (T={state.T})")

            # 싸이클 종료 체크
            cycle_ended = check_cycle_end(session, pf, state, today_str)

            logger.info(f"  → T={state.T}, ☆%={state.star_pct:.1f}%, "
                       f"avg=${state.avg_price:.2f}, qty={state.qty}, "
                       f"매수누적=${state.cum_buy_amount:.2f}, 매도누적=${state.cum_sell_amount:.2f}")

            session.commit()

        # ===== 최종 결과 =====
        logger.info(f"\n{'='*60}")
        logger.info(f"  시뮬레이션 완료!")
        logger.info(f"  최종 상태: T={state.T}, qty={state.qty}, avg=${state.avg_price:.2f}")
        logger.info(f"  모드: {state.mode}, 매수누적: ${state.cum_buy_amount:,.2f}, 매도누적: ${state.cum_sell_amount:,.2f}")
        logger.info(f"  싸이클: #{getattr(pf, 'current_cycle', 1)}")

        total_trades = session.scalar(
            select(func.count(Trade.id)).where(Trade.portfolio_id == pf.id)
        )
        total_cycles = session.scalar(
            select(func.count(CycleHistory.id)).where(CycleHistory.portfolio_id == pf.id)
        )
        logger.info(f"  DB 기록: 거래 {total_trades}건, 완료 싸이클 {total_cycles}건")
        logger.info(f"  대시보드에서 확인: http://localhost:8000")
        logger.info(f"{'='*60}")


if __name__ == "__main__":
    from sqlalchemy import func

    parser = argparse.ArgumentParser(description="무한매수법 V2.2 시뮬레이터")
    parser.add_argument("--ticker", default="SIM_TEST", help="시뮬레이션 티커 (기본: SIM_TEST)")
    parser.add_argument("--seed", type=float, default=4000, help="시드 금액 (기본: $4000)")
    parser.add_argument("--A", type=int, default=40, help="분할 일수 (기본: 40)")
    parser.add_argument("--R", type=float, default=10, help="목표수익률 %% (기본: 10)")
    parser.add_argument("--price", type=float, default=50, help="시작 주가 (기본: $50)")
    parser.add_argument("--days", type=int, default=50, help="시뮬레이션 일수 (기본: 50)")
    parser.add_argument("--scenario", default="default",
                        choices=["default", "crash", "recovery"],
                        help="시나리오: default(하락→반등), crash(폭락), recovery(V자)")
    parser.add_argument("--reset", action="store_true", help="기존 시뮬 데이터 초기화")
    args = parser.parse_args()

    run_simulation(
        ticker=args.ticker,
        seed=args.seed,
        A=args.A,
        R=args.R,
        start_price=args.price,
        days=args.days,
        scenario=args.scenario,
        reset=args.reset,
    )
