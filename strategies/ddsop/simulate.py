# -*- coding: utf-8 -*-
"""
떨사오팔 v1 로컬 시뮬레이터

실제 KIS API 없이 가상 주가로 트렌치 매매 흐름을 테스트합니다.
- 주문 생성: trading_logic.py의 generate_orders 그대로 사용
- 체결 판단: 가상 종가 vs 주문가격 비교 (LOC/MOC)
- DB 기록: trades, tranches 등 sim DB에 적재
- 대시보드에서 결과 확인 가능

DB: ddsop_sim.db (실제 거래 DB와 완전 분리)

사용법:
  python simulate.py                          # 3개 시나리오 전체 실행
  python simulate.py --scenario 1             # 시나리오 1만
  python simulate.py --total 5000 --n 10 --x 3  # 커스텀 설정

대시보드에서 시뮬 결과 확인:
  set DATABASE_URL=sqlite:///ddsop_sim.db
  python main.py
"""
import argparse
import logging
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from .config import BASE_DIR
from .models import (
    Base, Ticker, Tranche, Trade, CycleHistory, TrancheStatus,
)
from .trading_logic import generate_orders, find_next_buy_tranche, OrderItem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("simulator")

SIM_DB_URL = f"sqlite:///{BASE_DIR / 'ddsop_sim.db'}"


# ========== 가격 생성 (고정 패턴, 랜덤 없음) ==========
def generate_prices(start_price: float, segments: list[tuple[str, int]],
                    daily_pct: float = 3.0) -> list[dict]:
    """
    segments: [("down", 20), ("up", 20), ...] 형태
    매일 정확히 daily_pct% 씩 하락/상승
    """
    prices = []
    price = start_price
    day = 0
    for direction, num_days in segments:
        for _ in range(num_days):
            day += 1
            if direction == "down":
                price = price * (1 - daily_pct / 100)
            else:
                price = price * (1 + daily_pct / 100)
            price = round(price, 4)
            close = round(price, 2)
            prices.append({
                "day": day,
                "close": close,
                "low": round(close * 0.995, 2),
                "high": round(close * 1.005, 2),
            })
    return prices


SCENARIOS = {
    1: {
        "name": "20일하락 → 20일상승 → 40일하락 → 40일상승",
        "segments": [("down", 20), ("up", 20), ("down", 40), ("up", 40)],
    },
    2: {
        "name": "10일하락 → 10일상승 → 40일하락 → 40일상승",
        "segments": [("down", 10), ("up", 10), ("down", 40), ("up", 40)],
    },
    3: {
        "name": "10일상승 → 10일하락 → 20일하락 → 20일상승",
        "segments": [("up", 10), ("down", 10), ("down", 20), ("up", 20)],
    },
    4: {
        "name": "5일상승 → 5일하락 x10회 반복",
        "segments": [("up", 5), ("down", 5)] * 10,
    },
}


# ========== 체결 판단 ==========
def check_execution(order: OrderItem, price_data: dict) -> dict | None:
    """
    LOC매수: 종가 <= 주문가이면 종가로 체결
    LOC매도: 종가 >= 주문가이면 종가로 체결
    MOC: 종가로 무조건 체결
    """
    close = price_data["close"]

    if order.order_type == "MOC":
        return {"price": close, "qty": order.qty}

    if order.order_type == "LOC":
        if order.side == "buy":
            if close <= order.price:
                return {"price": close, "qty": order.qty}
        else:
            if close >= order.price:
                return {"price": close, "qty": order.qty}
    return None


# ========== 시뮬레이션 1회 실행 ==========
def run_one_scenario(
    scenario_num: int,
    total_usd: float = 5000.0,
    num_tranches: int = 10,
    x_pct: float = 3.0,
    start_price: float = 50.0,
    daily_pct: float = 3.0,
) -> dict:
    """시나리오 1개 실행, 결과 dict 반환"""

    sc = SCENARIOS[scenario_num]
    ticker_name = f"SIM_S{scenario_num}"

    engine = create_engine(SIM_DB_URL)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        # 기존 시뮬 데이터 초기화
        existing = session.scalar(select(Ticker).where(Ticker.ticker == ticker_name))
        if existing:
            session.delete(existing)
            session.commit()

        # Ticker + Tranche 생성
        tk = Ticker(
            ticker=ticker_name, total_usd=total_usd,
            num_tranches=num_tranches, x_pct=x_pct,
            is_active=True, trading_enabled=True,
        )
        session.add(tk)
        session.flush()

        amt_per = total_usd / num_tranches
        tranches_list = []
        for i in range(1, num_tranches + 1):
            t = Tranche(
                ticker_id=tk.id, tranche_num=i,
                amount_per_tranche=round(amt_per, 2),
            )
            session.add(t)
            tranches_list.append(t)
        session.commit()
        for t in tranches_list:
            session.refresh(t)
        session.refresh(tk)

        prices = generate_prices(start_price, sc["segments"], daily_pct)
        base_date = datetime(2026, 1, 5)

        logger.info("")
        logger.info("=" * 70)
        logger.info(f"  시나리오 {scenario_num}: {sc['name']}")
        logger.info(f"  투자금: ${total_usd:,.0f} | N={num_tranches}트렌치 | x={x_pct}%")
        logger.info(f"  트렌치당: ${amt_per:,.0f} | 시작가: ${start_price:.2f} | 일변동: {daily_pct}%")
        logger.info(f"  기간: {len(prices)}거래일")
        logger.info("=" * 70)

        loss_cut_count = 0
        cycle_count = 0
        total_buy_amount = 0.0
        total_sell_amount = 0.0
        realized_pnl = 0.0
        prev_close = start_price

        for price_data in prices:
            day = price_data["day"]
            close = price_data["close"]
            current_date = base_date + timedelta(days=day)
            today_str = current_date.strftime("%Y%m%d")

            # 트렌치 새로 로드
            tranches = session.scalars(
                select(Tranche).where(Tranche.ticker_id == tk.id).order_by(Tranche.tranche_num)
            ).all()

            # 주문 생성 (전일 종가 기준으로 주문가 산출)
            orders = generate_orders(tk, tranches, prev_close, today_str)

            bought_count = sum(1 for t in tranches if t.status == TrancheStatus.BOUGHT.value)
            idle_count = sum(1 for t in tranches if t.status == TrancheStatus.IDLE.value)

            day_buys = []
            day_sells = []
            day_losscuts = []

            for order in orders:
                exec_result = check_execution(order, price_data)
                marker = "●" if exec_result else "○"
                side_kr = "매수" if order.side == "buy" else "매도"
                ot = order.order_type
                if order.order_type == "MOC" and order.side == "sell":
                    ot = "MOC(손절)"

                if exec_result:
                    ep = exec_result["price"]
                    eq = exec_result["qty"]
                    amount = round(ep * eq, 2)
                    tranche = session.get(Tranche, order.tranche_id)

                    if order.side == "buy":
                        tranche.status = TrancheStatus.BOUGHT.value
                        tranche.avg_price = ep
                        tranche.qty = eq
                        tranche.buy_price = ep
                        tranche.buy_date = today_str
                        tranche.days_held = 0
                        total_buy_amount += amount
                        day_buys.append(f"T{order.tranche_num} ${ep:.2f}x{eq}")
                    else:
                        pnl = round(amount - (tranche.avg_price * tranche.qty), 2)
                        total_sell_amount += amount
                        realized_pnl += pnl
                        if order.order_type == "MOC":
                            loss_cut_count += 1
                            day_losscuts.append((order.tranche_num, f"T{order.tranche_num} ${ep:.2f}x{eq} (손익${pnl:+.2f})"))
                        else:
                            day_sells.append((order.tranche_num, f"T{order.tranche_num} ${ep:.2f}x{eq} (손익${pnl:+.2f})"))

                        tranche.status = TrancheStatus.IDLE.value
                        tranche.avg_price = 0.0
                        tranche.qty = 0
                        tranche.buy_price = 0.0
                        tranche.buy_date = ""
                        tranche.days_held = 0

                    session.add(Trade(
                        tranche_id=tranche.id, ticker=ticker_name,
                        tranche_num=order.tranche_num,
                        side=order.side, order_type=order.order_type,
                        price=ep, qty=eq, amount=amount,
                        trade_date=today_str,
                    ))

            # 싸이클 종료 확인
            tranches = session.scalars(
                select(Tranche).where(Tranche.ticker_id == tk.id).order_by(Tranche.tranche_num)
            ).all()

            # 싸이클 종료: T1이 오늘 LOC매도(이익실현)로 IDLE이 된 경우만
            t1_loc_sold_today = any(num == 1 for num, _ in day_sells)
            cycle_end_info = None
            if t1_loc_sold_today:
                cycle_sell_amt = total_sell_amount
                cycle_buy_amt = total_buy_amount
                profit = round(realized_pnl, 2)
                profit_pct = round((profit / cycle_buy_amt * 100) if cycle_buy_amt > 0 else 0, 2)

                all_trades = session.scalars(
                    select(Trade).where(Trade.ticker == ticker_name)
                ).all()
                start_dates = [t.trade_date for t in all_trades if t.trade_date]
                sd = min(start_dates) if start_dates else today_str

                session.add(CycleHistory(
                    ticker_id=tk.id, ticker=ticker_name,
                    cycle_number=tk.current_cycle,
                    start_date=sd, end_date=today_str,
                    total_buy_amount=round(cycle_buy_amt, 2),
                    total_sell_amount=round(cycle_sell_amt, 2),
                    profit=profit, profit_pct=profit_pct,
                ))
                cycle_end_info = (tk.current_cycle, profit, profit_pct)
                cycle_count += 1
                tk.current_cycle += 1
                realized_pnl = 0.0
                total_buy_amount = 0.0
                total_sell_amount = 0.0
                for trn in tranches:
                    trn.cycle_number = tk.current_cycle

            # 보유일수 증가
            for t in tranches:
                if t.status == TrancheStatus.BOUGHT.value:
                    t.days_held += 1

            session.commit()

            # 로그 출력
            events = []
            if day_buys:
                events.append(f"매수[{', '.join(day_buys)}]")
            if day_sells:
                events.append(f"매도[{', '.join(txt for _, txt in day_sells)}]")
            if day_losscuts:
                events.append(f"손절[{', '.join(txt for _, txt in day_losscuts)}]")

            tranches = session.scalars(
                select(Tranche).where(Tranche.ticker_id == tk.id).order_by(Tranche.tranche_num)
            ).all()
            bought_now = sum(1 for t in tranches if t.status == TrancheStatus.BOUGHT.value)

            status_str = ""
            for t in tranches:
                if t.status == TrancheStatus.BOUGHT.value:
                    status_str += f" T{t.tranche_num}(${t.avg_price:.2f},{t.days_held}d)"

            if events:
                logger.info(f"Day {day:3d} | ${close:7.2f} | 보유 {bought_now}/{num_tranches} | "
                            f"{' '.join(events)}")
            elif day % 10 == 0 or day == 1:
                logger.info(f"Day {day:3d} | ${close:7.2f} | 보유 {bought_now}/{num_tranches} |"
                            f"{status_str}")

            if cycle_end_info:
                cn, cp, cpp = cycle_end_info
                logger.info(f"  {'='*50}")
                logger.info(f"  싸이클 #{cn} 종료! (T1 LOC매도) "
                            f"실현수익 ${cp:+,.2f} ({cpp:+.2f}%)")
                logger.info(f"  {'='*50}")

            prev_close = close

        # 최종 상태
        tranches = session.scalars(
            select(Tranche).where(Tranche.ticker_id == tk.id).order_by(Tranche.tranche_num)
        ).all()
        final_bought = sum(1 for t in tranches if t.status == TrancheStatus.BOUGHT.value)
        unrealized = 0.0
        final_close = prices[-1]["close"]
        for t in tranches:
            if t.status == TrancheStatus.BOUGHT.value:
                unrealized += (final_close - t.avg_price) * t.qty
        unrealized = round(unrealized, 2)

        all_sell_trades = session.scalars(
            select(Trade).where(Trade.ticker == ticker_name, Trade.side == "sell")
        ).all()
        realized = 0.0
        for st in all_sell_trades:
            buy_t = session.scalar(
                select(Trade).where(
                    Trade.tranche_id == st.tranche_id,
                    Trade.side == "buy",
                    Trade.trade_date <= st.trade_date,
                ).order_by(Trade.trade_date.desc())
            )
            if buy_t:
                realized += st.amount - (buy_t.price * st.qty)
        realized = round(realized, 2)
        total_pnl = round(realized + unrealized, 2)

        total_trades = session.scalar(
            select(func.count(Trade.id)).where(Trade.ticker == ticker_name)
        ) or 0
        total_cycles_db = session.scalar(
            select(func.count(CycleHistory.id)).where(CycleHistory.ticker_id == tk.id)
        ) or 0

        logger.info("")
        logger.info(f"  --- 시나리오 {scenario_num} 최종 결과 ---")
        logger.info(f"  기간: {len(prices)}거래일")
        logger.info(f"  총 매수금: ${total_buy_amount:,.2f}")
        logger.info(f"  총 매도금: ${total_sell_amount:,.2f}")
        logger.info(f"  실현 손익: ${realized:+,.2f}")
        logger.info(f"  미실현 손익: ${unrealized:+,.2f} (잔여 {final_bought}트렌치 × 종가 ${final_close:.2f})")
        logger.info(f"  합계 손익: ${total_pnl:+,.2f}")
        logger.info(f"  완료 싸이클: {total_cycles_db}회")
        logger.info(f"  손절 횟수: {loss_cut_count}회")
        logger.info(f"  체결 건수: {total_trades}건")
        logger.info("")

        return {
            "scenario": scenario_num,
            "name": sc["name"],
            "days": len(prices),
            "total_buy": total_buy_amount,
            "total_sell": total_sell_amount,
            "realized": realized,
            "unrealized": round(unrealized, 2),
            "total_pnl": total_pnl,
            "cycles": total_cycles_db,
            "loss_cuts": loss_cut_count,
            "trades": total_trades,
            "final_close": final_close,
            "final_bought": final_bought,
        }


# ========== 비교 요약 ==========
def print_comparison(results: list[dict]):
    logger.info("")
    logger.info("=" * 90)
    logger.info("  시나리오 비교 요약")
    logger.info("=" * 90)
    header = f"{'':>5} {'시나리오':<40} {'기간':>5} {'실현손익':>12} {'미실현':>12} {'합계':>12} {'싸이클':>6} {'손절':>4}"
    logger.info(header)
    logger.info("-" * 90)
    for r in results:
        line = (f"  S{r['scenario']}  {r['name']:<38} "
                f"{r['days']:>4}일 "
                f"${r['realized']:>+10,.2f} "
                f"${r['unrealized']:>+10,.2f} "
                f"${r['total_pnl']:>+10,.2f} "
                f"{r['cycles']:>5}회 "
                f"{r['loss_cuts']:>3}회")
        logger.info(line)
    logger.info("=" * 90)


# ========== 메인 ==========
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="떨사오팔 v1 시뮬레이터")
    parser.add_argument("--scenario", type=int, default=0,
                        help="시나리오 번호 (1/2/3, 0=전체)")
    parser.add_argument("--total", type=float, default=5000,
                        help="총 투자금 USD (기본: 5000)")
    parser.add_argument("--n", type=int, default=10,
                        help="트렌치 수 (기본: 10)")
    parser.add_argument("--x", type=float, default=3.0,
                        help="매수/매도 기준 %% (기본: 3)")
    parser.add_argument("--price", type=float, default=50.0,
                        help="시작 주가 (기본: 50)")
    parser.add_argument("--daily", type=float, default=3.0,
                        help="일일 변동률 %% (기본: 3)")
    args = parser.parse_args()

    scenarios_to_run = [args.scenario] if args.scenario in SCENARIOS else sorted(SCENARIOS.keys())
    results = []

    for sn in scenarios_to_run:
        r = run_one_scenario(
            scenario_num=sn,
            total_usd=args.total,
            num_tranches=args.n,
            x_pct=args.x,
            start_price=args.price,
            daily_pct=args.daily,
        )
        results.append(r)

    if len(results) > 1:
        print_comparison(results)
