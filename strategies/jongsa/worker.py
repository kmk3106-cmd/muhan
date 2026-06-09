# -*- coding: utf-8 -*-
"""
종사종팔 v1 - 일일 실행 엔진 (워커)
장개시 30분 후 1회 실행:
  1) KIS 토큰 인증
  2) 전일 주문 결과 동기화 (체결 확인 → 트렌치 상태 반영)
  3) 계좌 요약 업데이트
  4) 종목별 주문 생성 (trading_logic)
  5) KIS API 주문 제출
  6) 보유일수 업데이트
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import create_engine, select, and_
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL, TRADING_MODE, CTAC_TLNO, KIS_DEVL_YAML, KILL_SWITCH_FILE
from .kis_client import KISClient, get_shared_client as _kis_get_shared, reset_shared_client, ORD_DVSN_LOC, ORD_DVSN_MOC
from .models import (
    init_db, Ticker, Tranche, TradeOrder, Trade, CycleHistory, AppLog, TrancheStatus,
)
from .settings_store import get_kis_settings, save_account_summary, get_account_summary
from .trading_logic import generate_orders, find_next_buy_tranche

logger = logging.getLogger(__name__)
_worker_lock = threading.Lock()

ET = ZoneInfo("US/Eastern")
KST = ZoneInfo("Asia/Seoul")


def _ts_kst() -> str:
    """KST 기준 시각 (HH:MM:SS)"""
    try:
        return datetime.now(KST).strftime("%H:%M:%S")
    except Exception:
        return datetime.now().strftime("%H:%M:%S")


def _log_db(session: Session, level: str, msg: str):
    """DB에 UTC로 저장 → API에서 KST로 변환 표시"""
    session.add(AppLog(level=level, message=msg, created_at=datetime.now(timezone.utc)))
    session.commit()


def _log_structured(session: Session, level: str, ticker: str, category: str, detail: str):
    """구조화 로그: [HH:MM:SS] [TICKER] [구분] 상세 (종목별·시간별 구분)"""
    msg = f"[{_ts_kst()}] [{ticker}] [{category}] {detail}"
    _log_db(session, level, msg)


def _today_kst() -> str:
    """KST 기준 오늘 날짜 (서버가 UTC여도 정확)"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    except Exception:
        try:
            import pytz
            return datetime.now(pytz.timezone("Asia/Seoul")).strftime("%Y%m%d")
        except Exception:
            return datetime.now().strftime("%Y%m%d")


def _yesterday_kst() -> str:
    """KST 기준 어제 날짜 (자정 경계 중복주문 방지용)"""
    try:
        from zoneinfo import ZoneInfo
        n = datetime.now(ZoneInfo("Asia/Seoul"))
        return (n - timedelta(days=1)).strftime("%Y%m%d")
    except Exception:
        try:
            import pytz
            n = datetime.now(pytz.timezone("Asia/Seoul"))
            return (n - timedelta(days=1)).strftime("%Y%m%d")
        except Exception:
            return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")


def _us_market_holidays(year: int) -> set:
    """해당 연도의 NYSE 휴장일 집합 반환"""
    from datetime import date
    holidays = set()

    def _observed(d: date) -> date:
        if d.weekday() == 5:  # Saturday → Friday
            return d - timedelta(days=1)
        if d.weekday() == 6:  # Sunday → Monday
            return d + timedelta(days=1)
        return d

    def _nth_weekday(y, m, n, wd):
        d = date(y, m, 1)
        count = 0
        while True:
            if d.weekday() == wd:
                count += 1
                if count == n:
                    return d
            d += timedelta(days=1)

    def _last_weekday(y, m, wd):
        if m == 12:
            d = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            d = date(y, m + 1, 1) - timedelta(days=1)
        while d.weekday() != wd:
            d -= timedelta(days=1)
        return d

    # New Year's Day
    holidays.add(_observed(date(year, 1, 1)))
    # MLK Day (3rd Monday Jan)
    holidays.add(_nth_weekday(year, 1, 3, 0))
    # Presidents' Day (3rd Monday Feb)
    holidays.add(_nth_weekday(year, 2, 3, 0))
    # Good Friday (Easter - 2)
    holidays.add(_easter(year) - timedelta(days=2))
    # Memorial Day (last Monday May)
    holidays.add(_last_weekday(year, 5, 0))
    # Juneteenth
    holidays.add(_observed(date(year, 6, 19)))
    # Independence Day
    holidays.add(_observed(date(year, 7, 4)))
    # Labor Day (1st Monday Sep)
    holidays.add(_nth_weekday(year, 9, 1, 0))
    # Thanksgiving (4th Thursday Nov)
    holidays.add(_nth_weekday(year, 11, 4, 3))
    # Christmas
    holidays.add(_observed(date(year, 12, 25)))

    return holidays


def _easter(year: int):
    """Anonymous Gregorian Easter algorithm"""
    from datetime import date
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _count_trading_days(buy_date_str: str, today_str: str) -> int:
    """buy_date부터 today까지 미국 거래일 수 (주말 + NYSE 휴장일 제외)"""
    from datetime import date
    try:
        bd = datetime.strptime(buy_date_str, "%Y%m%d").date()
        td = datetime.strptime(today_str, "%Y%m%d").date()
    except (ValueError, TypeError):
        return 0
    if td <= bd:
        return 0
    years = set(range(bd.year, td.year + 1))
    holidays = set()
    for y in years:
        holidays |= _us_market_holidays(y)
    count = 0
    d = bd + timedelta(days=1)
    while d <= td:
        if d.weekday() < 5 and d not in holidays:
            count += 1
        d += timedelta(days=1)
    return count


def is_kill_switch_on() -> bool:
    return KILL_SWITCH_FILE.exists()


def kill_switch_activate():
    KILL_SWITCH_FILE.write_text("ON")
    logger.warning("Kill Switch 활성화")


def kill_switch_deactivate():
    if KILL_SWITCH_FILE.exists():
        KILL_SWITCH_FILE.unlink()
    logger.info("Kill Switch 해제")


def get_us_market_run_time_kst() -> tuple[int, int]:
    """미국장 개시 30분 후 KST 시각 계산 (써머타임 자동 반영)"""
    now_et = datetime.now(ET)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    run_et = market_open + timedelta(minutes=30)
    run_kst = run_et.astimezone(KST)
    return run_kst.hour, run_kst.minute


def get_next_worker_run_kst() -> str:
    """다음 워커 실행 예정 시각 (KST) 문자열"""
    run_h, run_m = get_us_market_run_time_kst()
    now_kst = datetime.now(KST)
    next_run = now_kst.replace(hour=run_h, minute=run_m, second=0, microsecond=0)
    if next_run <= now_kst:
        next_run += timedelta(days=1)
    return next_run.strftime("%m/%d %H:%M") + " KST"


def get_shared_client() -> tuple[KISClient, str]:
    """공유 클라이언트 반환 (kis_client 싱글톤, 1분 토큰 제한 회피)"""
    cfg = get_kis_settings(DATABASE_URL)
    if cfg:
        trading_mode = cfg.get("trading_mode") or TRADING_MODE
        ctac = cfg.get("ctac_tlno") or CTAC_TLNO
        client = _kis_get_shared(config_dict=cfg, env_dv=trading_mode)
    else:
        client = _kis_get_shared(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)
        ctac = CTAC_TLNO
    return client, ctac


def refresh_shared_client():
    """설정 변경 시 클라이언트 재생성 (설정 저장 후 호출)"""
    reset_shared_client()


def _normalize_odno(s: str) -> str:
    """주문번호 정규화 (앞 0 제거) - 비교용"""
    return str(s or "").strip().lstrip("0") or "0"


def _get_kis_pending_odnos(client: KISClient, ticker: str, ctac_tlno: str) -> set[str]:
    """KIS 미체결 조회 후 해당 종목의 odno 집합 반환. 빈 집합 = 미체결 없음."""
    odnos = set()
    nccs_parts = []
    for ovrs in ["NASD", "NYSE", "AMEX"]:
        df = client.inquire_nccs(ovrs_excg_cd=ovrs, ctac_tlno=ctac_tlno)
        if not df.empty:
            nccs_parts.append(df)
    if not nccs_parts:
        return odnos
    df = pd.concat(nccs_parts, ignore_index=True)
    odno_col = next((c for c in df.columns if str(c).upper().replace("_", "") in ("ODNO", "ORGNODNO")), None)
    if odno_col:
        df = df.drop_duplicates(subset=[odno_col], keep="first")
    pdno_col = next((c for c in df.columns if str(c).upper().replace("_", "") == "PDNO"), "pdno")
    for _, row in df.iterrows():
        row_pdno = str(row.get(pdno_col, row.get("PDNO", "")) or "").strip()
        if row_pdno != ticker:
            continue
        raw = str(row.get("odno", row.get("ORGN_ODNO", row.get("ODNO", ""))) or "").strip()
        if raw:
            odnos.add(raw)
            odnos.add(_normalize_odno(raw))
    return odnos


def _get_kis_pending_order_keys(client: KISClient, ticker: str, ctac_tlno: str) -> set[tuple]:
    """
    KIS 미체결 조회 후 해당 종목의 (side, price, qty) 집합 반환.
    DB에 없어도 KIS에 이미 있는 주문은 중복 제출 방지용.
    """
    keys = set()
    nccs_parts = []
    for ovrs in ["NASD", "NYSE", "AMEX"]:
        df = client.inquire_nccs(ovrs_excg_cd=ovrs, ctac_tlno=ctac_tlno)
        if not df.empty:
            nccs_parts.append(df)
    if not nccs_parts:
        return keys
    df = pd.concat(nccs_parts, ignore_index=True)
    odno_col = next((c for c in df.columns if str(c).upper().replace("_", "") in ("ODNO", "ORGNODNO")), None)
    if odno_col:
        df = df.drop_duplicates(subset=[odno_col], keep="first")
    pdno_col = next((c for c in df.columns if str(c).upper().replace("_", "") == "PDNO"), "pdno")
    for _, row in df.iterrows():
        row_pdno = str(row.get(pdno_col, row.get("PDNO", "")) or "").strip()
        if row_pdno != ticker:
            continue
        side_cd = str(row.get("sll_buy_dvsn", row.get("SLL_BUY_DVSN", "")) or "").strip()
        side = "buy" if side_cd in ("02", "2") else "sell"
        try:
            price_raw = row.get("ord_unpr", row.get("ovrs_ord_unpr", row.get("ORD_UNPR", row.get("OVRS_ORD_UNPR", 0)))) or 0
            price = round(float(price_raw), 2) if price_raw else 0.0
        except (TypeError, ValueError):
            price = 0.0
        try:
            qty_raw = row.get("nccs_qty", row.get("NCCS_QTY", row.get("ord_qty", row.get("ORD_QTY", 0)))) or 0
            qty = int(float(qty_raw)) if qty_raw else 0
        except (TypeError, ValueError):
            qty = 0
        if price > 0 and qty > 0:
            keys.add((side, price, qty))
    return keys


def _sync_pending_with_kis(session: Session, client: KISClient, ticker_obj: Ticker,
                           today_str: str, ctac_tlno: str) -> int:
    """
    DB의 pending/submitted 주문 중 KIS 미체결에 없는 건 → cancelled 처리.
    외부(다른 HTS/API)에서 취소된 경우 재제출 가능하도록.
    order_date는 today+ yesterday 포함 (자정 경계 주문 포함)
    반환: cancelled로 갱신한 건수
    """
    yesterday_str = _yesterday_kst()
    kis_odnos = _get_kis_pending_odnos(client, ticker_obj.ticker, ctac_tlno)
    db_pending = session.scalars(
        select(TradeOrder).where(
            TradeOrder.ticker == ticker_obj.ticker,
            TradeOrder.order_date.in_([today_str, yesterday_str]),
            TradeOrder.status.in_(["pending", "submitted"]),
            TradeOrder.kis_order_no != "",
        )
    ).all()
    updated = 0
    for o in db_pending:
        kno = str(o.kis_order_no or "").strip()
        if not kno:
            continue
        # kno 또는 정규화된 kno가 KIS 미체결에 있으면 유지
        if kno in kis_odnos or _normalize_odno(kno) in kis_odnos:
            continue
        o.status = "cancelled"
        updated += 1
        side_kr = "매수" if o.side == "buy" else "매도"
        _log_structured(session, "INFO", ticker_obj.ticker, "미체결",
                        f"DB주문 #{kno} ({side_kr}) → KIS 미체결 없음, cancelled 처리 (외부취소)")
        logger.info(f"  [{ticker_obj.ticker}] #{kno} cancelled (KIS에 미체결 없음)")
    if updated:
        session.commit()
    return updated


def _sync_executions(session: Session, client: KISClient, ticker_obj: Ticker,
                     tranches: list[Tranche], ctac_tlno: str, today_str: str):
    """
    체결된 주문만 조회하여 트렌치 상태를 업데이트.
    - ccld_nccs_dvsn="01" → 체결만 조회 (미체결 제외)
    - 매수 체결 → 트렌치 BOUGHT, avg_price/qty/buy_date 기록
    - 매도 체결 → 트렌치 IDLE 리셋, Trade 기록
    - 전날 체결은 KIS 정산/조회 시점에 따라 다음날 반영될 수 있음
    """
    start_dt = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    dt_recent = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
    end_dt = today_str

    def _fetch_ccnl(sdt: str, edt: str):
        parts = []
        for ovrs in ["NASD", "NYSE", "AMEX"]:
            df = client.inquire_ccnl(
                pdno=ticker_obj.ticker, ord_strt_dt=sdt, ord_end_dt=edt,
                ccld_nccs_dvsn="01", ovrs_excg_cd=ovrs, ctac_tlno=ctac_tlno,
            )
            if not df.empty:
                parts.append(df)
        if not parts:
            return client.inquire_ccnl(
                pdno=ticker_obj.ticker, ord_strt_dt=sdt, ord_end_dt=edt,
                ccld_nccs_dvsn="01", ovrs_excg_cd="%", ctac_tlno=ctac_tlno,
            )
        out = pd.concat(parts, ignore_index=True)
        if len(parts) > 1:
            odno_col = next((c for c in ["odno", "ODNO", "ORGN_ODNO"] if c in out.columns), None)
            if odno_col:
                out = out.drop_duplicates(subset=[odno_col], keep="first")
        return out

    ccnl_df = _fetch_ccnl(start_dt, end_dt)
    # 체결 많은 종목: 7일 조회만으로 최근 누락 가능 → 최근 3일 추가 조회 병합
    recent_df = _fetch_ccnl(dt_recent, end_dt)
    if not recent_df.empty and not ccnl_df.empty:
        key_cols = [c for c in ["odno", "ODNO", "ord_dt", "ORD_DT", "ft_ccld_qty", "ccld_qty"]
                   if c in ccnl_df.columns and c in recent_df.columns]
        if not key_cols:
            key_cols = [c for c in ["odno", "ODNO", "ORGN_ODNO"] if c in ccnl_df.columns and c in recent_df.columns]
        if key_cols:
            combined = pd.concat([ccnl_df, recent_df], ignore_index=True)
            ccnl_df = combined.drop_duplicates(subset=key_cols, keep="last")
    elif not recent_df.empty and ccnl_df.empty:
        ccnl_df = recent_df
    if ccnl_df.empty:
        logger.debug(f"  [{ticker_obj.ticker}] 체결내역(ccnl) 없음")
        return

    logger.info(f"  [{ticker_obj.ticker}] 체결내역 {len(ccnl_df)}건 조회")
    _log_structured(session, "INFO", ticker_obj.ticker, "체결조회", f"체결내역 {len(ccnl_df)}건 조회")

    tranche_map = {t.tranche_num: t for t in tranches}
    # pending 또는 submitted (구버전) 모두 체결 매칭 대상
    pending_orders = session.scalars(
        select(TradeOrder).where(
            TradeOrder.ticker == ticker_obj.ticker,
            TradeOrder.status.in_(["pending", "submitted"]),
        )
    ).all()
    order_map = {o.kis_order_no: o for o in pending_orders if o.kis_order_no}
    if not order_map and not ccnl_df.empty:
        logger.warning(f"  [{ticker_obj.ticker}] ccnl {len(ccnl_df)}건 있으나 매칭할 pending/submitted 주문 없음 "
                       f"(order_map 비어있음)")

    for _, row in ccnl_df.iterrows():
        odno = str(row.get("odno", row.get("ORGN_ODNO", row.get("ODNO", "")))).strip()
        if not odno:
            continue
        order = order_map.get(odno)
        if not order:
            continue
        if order.status not in ("pending", "submitted"):
            continue

        # KIS inquire-ccnl API 기준: ccld_unpr/ft_ccld_unpr3=체결단가, tot_ccld_amt는 주문금액일 수 있음
        # → 체결금액은 반드시 체결가×수량 사용 (참고: apiportal.koreainvestment.com/apiservice...inquire-ccnl)
        ccld_qty = int(float(row.get("ft_ccld_qty", 0) or row.get("ccld_qty", 0) or 0))
        ccld_prc = float(row.get("ft_ccld_unpr3", 0) or row.get("ccld_unpr", 0) or 0)
        if ccld_qty <= 0 or ccld_prc <= 0:
            continue

        tranche = session.get(Tranche, order.tranche_id)
        if not tranche:
            continue

        trade_date = str(row.get("ord_dt", today_str))
        amount = round(ccld_prc * ccld_qty, 2)

        cycle_no = tranche.cycle_number if tranche.cycle_number else 1
        session.add(Trade(
            tranche_id=tranche.id, ticker=ticker_obj.ticker,
            tranche_num=tranche.tranche_num, cycle_number=cycle_no,
            side=order.side, order_type=order.order_type,
            price=ccld_prc, qty=ccld_qty, amount=amount,
            trade_date=trade_date,
        ))
        order.status = "filled"

        if order.side == "buy":
            tranche.status = TrancheStatus.BOUGHT.value
            tranche.avg_price = ccld_prc
            tranche.qty = ccld_qty
            tranche.buy_price = ccld_prc
            tranche.buy_date = trade_date
            tranche.days_held = 0
            logger.info(f"  [{ticker_obj.ticker}] T{tranche.tranche_num} 매수 체결: "
                        f"${ccld_prc:.2f} x {ccld_qty}주")
            _log_structured(session, "INFO", ticker_obj.ticker, "주문체결",
                            f"T{tranche.tranche_num} 매수 체결: ${ccld_prc:.2f} x {ccld_qty}주")

        elif order.side == "sell":
            buy_amount = tranche.avg_price * tranche.qty if tranche.avg_price > 0 else 0
            sell_amount = amount
            profit = round(sell_amount - buy_amount, 2)
            logger.info(f"  [{ticker_obj.ticker}] T{tranche.tranche_num} 매도 체결: "
                        f"${ccld_prc:.2f} x {ccld_qty}주 (손익 ${profit:.2f})")
            _log_structured(session, "INFO", ticker_obj.ticker, "주문체결",
                            f"T{tranche.tranche_num} 매도 체결: ${ccld_prc:.2f} x {ccld_qty}주 (손익 ${profit:.2f})")

            tranche.status = TrancheStatus.IDLE.value
            tranche.avg_price = 0.0
            tranche.qty = 0
            tranche.buy_price = 0.0
            tranche.buy_date = ""
            tranche.days_held = 0

    session.commit()


def _check_and_record_cycle(session: Session, ticker_obj: Ticker, tranches: list[Tranche],
                            today_str: str, actual_cash: float | None = None):
    """
    싸이클 종료 조건: T1이 LOC매도(이익실현)로 IDLE이 된 경우만.
    손절(MOC)은 싸이클 종료가 아님 → 다시 매수하여 계속 진행.
    싸이클 종료 시: total_usd=보유현금, 모든 트렌치 amount_per_tranche 재배분.
    """
    t1 = next((t for t in tranches if t.tranche_num == 1), None)
    if not t1 or t1.status != TrancheStatus.IDLE.value:
        return

    t1_sell_trades = session.scalars(
        select(Trade).where(
            Trade.ticker == ticker_obj.ticker,
            Trade.tranche_num == 1,
            Trade.side == "sell",
        )
    ).all()
    if not t1_sell_trades:
        return

    last_sell = max(t1_sell_trades, key=lambda t: t.trade_date)
    if last_sell.trade_date < (datetime.now() - timedelta(days=10)).strftime("%Y%m%d"):
        return

    if last_sell.order_type == "MOC":
        return

    # 방금 끝난 싸이클 = T1 LOC 매도의 cycle_number. 0/None이면 1로 (fallback은 current_cycle 사용 금지)
    raw_cy = getattr(last_sell, "cycle_number", None)
    cycle_that_ended = raw_cy if (raw_cy is not None and raw_cy > 0) else 1
    existing_cycle = session.scalar(
        select(CycleHistory).where(
            CycleHistory.ticker_id == ticker_obj.id,
            CycleHistory.cycle_number == cycle_that_ended,
        )
    )
    if existing_cycle:
        return

    all_cycle_trades = session.scalars(
        select(Trade).where(
            Trade.ticker == ticker_obj.ticker,
            Trade.cycle_number == cycle_that_ended,
        )
    ).all()

    # 실현 손익만 계산: 매도된 트렌치의 (매도금 - 매수금)
    sell_trades = [t for t in all_cycle_trades if t.side == "sell"]
    realized_pnl = 0.0
    total_sell = 0.0
    total_buy_for_sold = 0.0
    for st in sell_trades:
        total_sell += st.amount
        matching_buy = session.scalar(
            select(Trade).where(
                Trade.tranche_id == st.tranche_id,
                Trade.side == "buy",
                Trade.trade_date <= st.trade_date,
            ).order_by(Trade.trade_date.desc())
        )
        if matching_buy:
            buy_cost = matching_buy.price * st.qty
            total_buy_for_sold += buy_cost
            realized_pnl += st.amount - buy_cost

    total_buy_all = sum(t.amount for t in all_cycle_trades if t.side == "buy")
    profit = round(realized_pnl, 2)
    profit_pct = round((profit / total_buy_for_sold * 100) if total_buy_for_sold > 0 else 0, 2)

    start_dates = [t.trade_date for t in all_cycle_trades if t.trade_date]
    start_date = min(start_dates) if start_dates else today_str
    # [정합 보강 2026-05-31] end_date = 싸이클 내 마지막 매도 일자
    # (today_str=종료처리 실행일이면, 같은 날 다음 싸이클 첫 매수가 있을 때
    # /api/cycles 상세에 다음 싸이클 거래가 끼어드는 표시 버그 발생 — infinite와 동일)
    sell_dates = [t.trade_date for t in sell_trades if t.trade_date]
    end_date = max(sell_dates) if sell_dates else today_str

    session.add(CycleHistory(
        ticker_id=ticker_obj.id, ticker=ticker_obj.ticker,
        cycle_number=cycle_that_ended,
        start_date=start_date, end_date=end_date,
        total_buy_amount=round(total_buy_all, 2),
        total_sell_amount=round(total_sell, 2),
        profit=profit, profit_pct=profit_pct,
    ))
    ticker_obj.current_cycle = cycle_that_ended + 1

    seed_reflect = getattr(ticker_obj, "seed_reflect_enabled", False) or False
    if seed_reflect and actual_cash is not None and actual_cash > 0:
        ticker_obj.total_usd = actual_cash
        amt_per = round(actual_cash / len(tranches), 2)
        for t in tranches:
            t.cycle_number = ticker_obj.current_cycle
            t.amount_per_tranche = amt_per
        logger.info(f"  [{ticker_obj.ticker}] 다음 싸이클 트렌치 할당: ${amt_per:.2f}/회 (씨드반영 ON, 보유현금 ${actual_cash:.2f})")
    else:
        amt_per = round(ticker_obj.total_usd / len(tranches), 2)
        for t in tranches:
            t.cycle_number = ticker_obj.current_cycle
            t.amount_per_tranche = amt_per
        if not seed_reflect:
            logger.info(f"  [{ticker_obj.ticker}] 다음 싸이클 트렌치 할당: ${amt_per:.2f}/회 (씨드반영 OFF, 투자금 ${ticker_obj.total_usd:.2f} 유지)")

    logger.info(f"  [{ticker_obj.ticker}] 싸이클 #{ticker_obj.current_cycle - 1} 종료! "
                f"수익 ${profit:.2f} ({profit_pct:.2f}%)")
    _log_structured(session, "INFO", ticker_obj.ticker, "싸이클종료",
                    f"싸이클 #{ticker_obj.current_cycle - 1} 종료: 수익 ${profit:.2f} ({profit_pct:.2f}%)")

    session.commit()


def _extract_account_summary(balance_df1, balance_df2) -> dict:
    """
    KIS 해외주식 잔고 API 응답에서 계좌 정보 추출
    (무한매수법 extract_account_summary 동일 로직)

    output1 (balance_df1) - 종목별:
      ovrs_stck_evlu_amt, frcr_evlu_pfls_amt, frcr_pchs_amt1
    output2 (balance_df2) - 계좌 요약:
      tot_evlu_pfls_amt, frcr_pchs_amt1, ovrs_tot_pfls, tot_pftrt
    """
    stock_evlu = 0.0
    buy_amt = 0.0
    pnl = 0.0
    pnl_rt = 0.0

    def _float(val):
        try:
            return float(val) if val is not None and str(val).strip() else 0.0
        except (TypeError, ValueError):
            return 0.0

    if not balance_df1.empty:
        if "ovrs_stck_evlu_amt" in balance_df1.columns:
            stock_evlu = balance_df1["ovrs_stck_evlu_amt"].apply(_float).sum()
        if "frcr_evlu_pfls_amt" in balance_df1.columns:
            pnl = balance_df1["frcr_evlu_pfls_amt"].apply(_float).sum()
        if "frcr_pchs_amt1" in balance_df1.columns:
            buy_amt = balance_df1["frcr_pchs_amt1"].apply(_float).sum()

    if not balance_df2.empty:
        row = balance_df2.iloc[0]
        if pnl == 0:
            pnl = _float(row.get("tot_evlu_pfls_amt", 0))
        if buy_amt <= 0:
            buy_amt = _float(row.get("frcr_pchs_amt1", 0))
            if buy_amt <= 0:
                buy_amt = _float(row.get("frcr_buy_amt_smtl1", 0))
        pnl_rt = _float(row.get("tot_pftrt", 0))

    if pnl_rt == 0 and buy_amt > 0 and pnl != 0:
        pnl_rt = round(pnl / buy_amt * 100, 2)
    if stock_evlu <= 0 and buy_amt > 0:
        stock_evlu = buy_amt + pnl

    return {
        "stock_evlu": round(stock_evlu, 2),
        "buy_amt": round(buy_amt, 2),
        "pnl": round(pnl, 2),
        "pnl_rt": round(pnl_rt, 2),
    }


def _update_account_summary(client: KISClient, ctac_tlno: str) -> tuple[bool, str]:
    """계좌 요약 정보 업데이트. (성공여부, 메시지) 반환"""
    try:
        df1, df2 = client.inquire_balance(ctac_tlno=ctac_tlno)
        acct = _extract_account_summary(df1, df2)
        acct.setdefault("cash", 0)
        acct.setdefault("tot_asst_amt", 0)
        acct.setdefault("exrt", 0)

        pb = client.inquire_present_balance(ctac_tlno=ctac_tlno)
        acct["cash"] = pb["deposit_usd"]
        exrt = pb["exrt"]
        acct["exrt"] = exrt
        tot_krw = pb["tot_asst_krw"]
        if exrt > 0 and tot_krw > 0:
            acct["tot_asst_amt"] = round(tot_krw / exrt, 2)
        else:
            acct["tot_asst_amt"] = round(acct["stock_evlu"] + acct["cash"], 2)

        if acct["stock_evlu"] > 0 or acct["buy_amt"] > 0 or acct["cash"] > 0:
            save_account_summary(acct)
            logger.info(f"  계좌요약 업데이트: 총액=${acct['tot_asst_amt']:.2f}, "
                        f"주식평가=${acct['stock_evlu']}, 예수금=${acct['cash']}")
        return True, f"계좌조회 API: 성공 ✓ (총액 ${acct['tot_asst_amt']:.2f})"
    except Exception as e:
        logger.warning(f"  계좌요약 업데이트 실패: {e}")
        return False, f"계좌조회 API: 실패 ✗ ({e})"


def _submit_orders(client: KISClient, session: Session, orders, ctac_tlno: str) -> tuple[int, int]:
    """주문 제출 및 DB 기록. (성공건수, 실패건수) 반환"""
    import random
    ok_count, fail_count = 0, 0
    prev_side = None
    for o in orders:
        if prev_side and prev_side != o.side:
            delay = random.uniform(15, 20)
            logger.info(f"  [{o.ticker}] 매도→매수 전환: {delay:.1f}초 대기 (cross 방지)")
            time.sleep(delay)
        ord_dvsn = ORD_DVSN_LOC if o.order_type == "LOC" else ORD_DVSN_MOC
        price_str = str(o.price) if o.order_type != "MOC" else "0"

        res = client.order(
            ord_dv=o.side, pdno=o.ticker,
            ord_qty=str(o.qty), ovrs_ord_unpr=price_str,
            ord_dvsn=ord_dvsn, ctac_tlno=ctac_tlno,
        )
        rt_cd = res.get("rt_cd", "1")
        odno = ""
        side_kr = "매수" if o.side == "buy" else "매도"
        if rt_cd == "0":
            ok_count += 1
            out = res.get("output", {})
            odno = out.get("ODNO", out.get("odno", ""))
            logger.info(f"  [{o.ticker}] T{o.tranche_num} {side_kr} {o.order_type} "
                        f"${o.price:.2f} x {o.qty}주 → 주문번호 {odno}")
            _log_structured(session, "INFO", o.ticker, "주문제출",
                            f"T{o.tranche_num} {side_kr} {o.order_type} ${o.price:.2f} x {o.qty}주 → {odno}")
        else:
            fail_count += 1
            msg1 = res.get("msg1", "") or "(오류메시지 없음)"
            msg_cd = res.get("msg_cd", "")
            err_detail = f"T{o.tranche_num} {side_kr} {o.order_type} ${o.price:.2f} x {o.qty}주 주문 거부: {msg1}"
            if msg_cd:
                err_detail += f" (코드: {msg_cd})"
            logger.error(f"[{o.ticker}] {err_detail} | KIS응답: {res}")
            _log_structured(session, "ERROR", o.ticker, "주문실패", err_detail)

        db_order = TradeOrder(
            tranche_id=o.tranche_id, ticker=o.ticker,
            side=o.side, order_type=o.order_type,
            price=o.price, qty=o.qty,
            status="pending" if rt_cd == "0" else "failed",  # pending=체결대기 (동기화 시 매칭)
            order_date=_today_kst(),
            kis_order_no=odno,
        )
        session.add(db_order)
        prev_side = o.side
        time.sleep(0.5)

    session.commit()
    return ok_count, fail_count


def run_worker_once(submit_orders: bool = True):
    """종사종팔 워커 1회 실행. submit_orders=False: 강제동기화 시 주문 제출 생략"""
    if not _worker_lock.acquire(blocking=False):
        logger.info("종사종팔 워커: 다른 작업 실행 중 - 건너뜀")
        return {"success": False, "tickers": [], "errors": ["다른 작업 실행 중 - 건너뜀"]}
    try:
        return _run_worker_once_impl(submit_orders)
    finally:
        _worker_lock.release()


def _run_worker_once_impl(submit_orders: bool = True):
    """워커 실제 로직 (락 획득 후 호출)"""
    logger.info("=" * 60)
    logger.info("종사종팔 워커 실행 시작" + (" (동기화만)" if not submit_orders else ""))

    engine = init_db(DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()

    quiet = not submit_orders

    def _log(msg: str, level: str = "INFO"):
        if quiet and level == "INFO":
            return
        _log_db(session, level, msg)

    if not quiet:
        _log("워커 실행 시작 (동기화 + 주문 제출)")

    if is_kill_switch_on():
        _log("다음 할 일: 대기 (Kill Switch ON - 거래 중단)")
        _log("워커 실행 스킵 (Kill Switch)")
        logger.warning("Kill Switch ON - 실행 중단")
        session.close()
        return {"success": False, "errors": ["Kill Switch 활성화 상태"]}

    client, ctac_tlno = get_shared_client()
    if not client.auth(ctac_tlno):
        _log("KIS API 인증: 실패 ✗", "ERROR")
        _log("워커 실행 완료: 0종목 (인증 실패)", "ERROR")
        logger.error("KIS 인증 실패")
        session.close()
        return {"success": False, "errors": ["KIS 인증 실패"]}

    _log("KIS API 인증: 성공 ✓")

    today_str = _today_kst()
    result = {"success": True, "tickers": [], "errors": []}

    try:
        acct_ok, acct_msg = _update_account_summary(client, ctac_tlno)
        _log(acct_msg if acct_ok else acct_msg, "WARNING" if not acct_ok else "INFO")
        acct_data = get_account_summary(DATABASE_URL)
        actual_cash = float(acct_data.get("cash", 0) or 0) if acct_data else 0.0

        tickers = session.scalars(
            select(Ticker).where(Ticker.is_active == True, Ticker.trading_enabled == True)
        ).all()

        if not tickers:
            _log("다음 할 일: 등록된 거래종목 없음 → 종목 추가 후 재실행")
            _log("워커 실행 완료: 0종목")
            logger.info("종사종팔 워커 실행 완료: 0종목")
            return result

        for ticker_obj in tickers:
            try:
                logger.info(f"\n--- {ticker_obj.ticker} (N={ticker_obj.num_tranches}, x={ticker_obj.x_pct}%) ---")
                tranches = session.scalars(
                    select(Tranche).where(Tranche.ticker_id == ticker_obj.id)
                ).all()
                if not tranches:
                    _log_structured(session, "WARNING", ticker_obj.ticker, "스킵", "다음 예정: 스킵 (트렌치 없음)")
                    logger.warning(f"  [{ticker_obj.ticker}] 트렌치 없음 - 스킵")
                    continue

                _sync_executions(session, client, ticker_obj, tranches, ctac_tlno, today_str)
                _check_and_record_cycle(session, ticker_obj, tranches, today_str, actual_cash)

                orders = []
                # 트렌치+매도매수별로 이미 미체결이 있으면 해당 주문만 생략 (동일 LOC 재제출 시 KIS가 기존 취소 방지)
                # 예: T1 매도만 있으면 T1 매도는 생략, T2 매수는 제출
                if submit_orders:
                    # DB pending 중 KIS 미체결에 없는 건 → cancelled (외부취소 반영)
                    cancelled_cnt = _sync_pending_with_kis(
                        session, client, ticker_obj, today_str, ctac_tlno
                    )
                    if cancelled_cnt:
                        _log_structured(session, "INFO", ticker_obj.ticker, "동기화",
                                        f"KIS 동기화: {cancelled_cnt}건 cancelled → 재제출 가능")
                    prev_close, _ = client.inquire_prev_close(ticker_obj.ticker, ctac_tlno=ctac_tlno)
                    price_src = "KIS"
                    if prev_close <= 0:
                        try:
                            import yfinance as yf
                            t = yf.Ticker(ticker_obj.ticker)
                            info = t.info
                            p = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
                            if p is not None:
                                fp = float(p)
                                if fp > 0:
                                    prev_close = fp
                                    price_src = "Yahoo(폴백)"
                                    _log_structured(session, "INFO", ticker_obj.ticker, "동기화",
                                                    f"KIS 실패 → Yahoo 가격 ${prev_close:.2f} 사용")
                        except Exception as e:
                            logger.debug(f"yfinance 폴백 실패 ({ticker_obj.ticker}): {e}")
                    if prev_close <= 0:
                        _log_structured(session, "ERROR", ticker_obj.ticker, "에러", "현재가 API: 실패 ✗ → 주문 스킵")
                        _log_structured(session, "WARNING", ticker_obj.ticker, "스킵", "다음 할 일: 다음 워커까지 대기 (가격 조회 실패)")
                        logger.warning(f"  [{ticker_obj.ticker}] 현재가 조회 실패 - 주문 스킵")
                        result["errors"].append(f"{ticker_obj.ticker}: 현재가 조회 실패")
                    else:
                        _log_structured(session, "INFO", ticker_obj.ticker, "동기화",
                                        f"가격 API: {'성공' if price_src == 'KIS' else 'Yahoo폴백'} ✓ (${prev_close:.2f})")
                        all_orders = generate_orders(ticker_obj, tranches, prev_close, today_str, actual_cash=actual_cash)
                        # 1) DB에 오늘/어제 pending인 (tranche_id, side) 조합은 제외 (자정 경계 중복 방지)
                        yesterday_str = _yesterday_kst()
                        existing_orders = session.scalars(
                            select(TradeOrder).where(
                                and_(
                                    TradeOrder.ticker == ticker_obj.ticker,
                                    TradeOrder.order_date.in_([today_str, yesterday_str]),
                                    TradeOrder.status.in_(["pending", "submitted"]),
                                )
                            )
                        ).all()
                        existing_pairs = {(o.tranche_id, o.side) for o in existing_orders}
                        orders = [o for o in all_orders if (o.tranche_id, o.side) not in existing_pairs]
                        # 2) KIS 미체결(실시간)에 이미 있는 (side, price, qty)는 제외 (DB 없는 경우도 중복 방지)
                        kis_pending_keys = _get_kis_pending_order_keys(client, ticker_obj.ticker, ctac_tlno)
                        before_kis = len(orders)
                        orders = [o for o in orders if (o.side, round(o.price, 2), o.qty) not in kis_pending_keys]
                        if before_kis > len(orders):
                            _log_structured(session, "INFO", ticker_obj.ticker, "미체결",
                                            f"KIS 미체결 {before_kis - len(orders)}건과 동일 → 제외 (신규 {len(orders)}건만 제출)")
                            logger.info(f"  [{ticker_obj.ticker}] KIS 미체결과 동일 {before_kis - len(orders)}건 제외")
                        # [종사종팔 옵션A 2026-06] 자전거래 회피 + 종가매수 유지:
                        # LOC매수 한도가 최저 익절매도가와 겹치면(>=) 매수를 '생략'하지 않고
                        # 한도를 '최저 익절가 바로 아래(×0.995)'로 낮춘다(캡).
                        # → 종가가 익절가 아래면 종가에 매수 체결, 익절가 위로 마감하면 익절이 체결.
                        #   둘이 같은 종가에 동시 체결될 일이 없어 자전거래 없음. (떨사오팔은 매수가<매도가라 캡 미발동)
                        sell_prices = [o.price for o in existing_orders if o.side == "sell" and o.price > 0]
                        sell_prices += [o.price for o in orders if o.side == "sell" and o.price > 0]
                        min_sell = min(sell_prices) if sell_prices else None
                        if min_sell is not None:
                            cap = round(min_sell * 0.995, 2)
                            for o in orders:
                                if o.side == "buy" and o.price > cap:
                                    logger.warning(f"  [{ticker_obj.ticker}] 자전거래 회피: LOC매수 한도 "
                                                   f"${o.price:.2f} → ${cap:.2f} 캡 (최저 익절가 ${min_sell:.2f} 아래, 매수 유지)")
                                    _log_structured(session, "INFO", ticker_obj.ticker, "캡",
                                                    f"LOC매수 한도 ${o.price:.2f}→${cap:.2f} (익절가 ${min_sell:.2f} 아래로 캡, 자전거래 회피·종가매수 유지)")
                                    o.price = cap
                        skipped = len(all_orders) - len(orders)
                        if skipped:
                            _log_structured(session, "INFO", ticker_obj.ticker, "미체결",
                                            f"기존 미체결 {skipped}건 유지 → 신규 {len(orders)}건만 제출")
                            logger.info(f"  [{ticker_obj.ticker}] 스킵 {skipped}건, 제출 {len(orders)}건")
                        if orders:
                            order_desc = ", ".join(
                                f"{'매수' if o.side == 'buy' else '매도'} T{o.tranche_num} {o.order_type}"
                                for o in orders
                            )
                            _log_structured(session, "INFO", ticker_obj.ticker, "주문제출",
                                            f"다음 예정: {order_desc} → KIS 주문 API 호출")
                            ok_n, fail_n = _submit_orders(client, session, orders, ctac_tlno)
                            if fail_n > 0:
                                _log_structured(session, "WARNING", ticker_obj.ticker, "주문실패",
                                                f"주문 API: {ok_n}건 성공, {fail_n}건 실패")
                            else:
                                _log_structured(session, "INFO", ticker_obj.ticker, "주문제출",
                                                f"주문 API: {ok_n}건 제출 성공 ✓")
                            _log_structured(session, "INFO", ticker_obj.ticker, "동기화",
                                            "다음 할 일: 미체결 체결 대기 (장중 LOC/MOC)")
                        elif all_orders:
                            _log_structured(session, "INFO", ticker_obj.ticker, "미체결",
                                            f"기존 미체결 {len(all_orders)}건 모두 유지 (추가 제출 없음)")
                            _log_structured(session, "INFO", ticker_obj.ticker, "동기화", "다음 할 일: 미체결 체결 대기")
                            logger.info(f"  [{ticker_obj.ticker}] 기존 미체결 유지")
                        else:
                            _log_structured(session, "INFO", ticker_obj.ticker, "스킵", "다음 예정: 오늘 주문 없음 (조건 미충족)")
                            _log_structured(session, "INFO", ticker_obj.ticker, "동기화", "다음 할 일: 다음 워커까지 대기")
                            logger.info(f"  [{ticker_obj.ticker}] 오늘 주문 없음")
                elif not submit_orders:
                    _log_structured(session, "INFO", ticker_obj.ticker, "동기화",
                                    "동기화만 모드: 주문 제출 안 함. 주문 제출하려면 '전체 실행' 버튼 사용")
                    logger.info(f"  [{ticker_obj.ticker}] 동기화만 - 주문 제출 생략")

                for t in tranches:
                    if t.status == TrancheStatus.BOUGHT.value and t.buy_date:
                        try:
                            t.days_held = _count_trading_days(t.buy_date, today_str)
                        except Exception:
                            pass
                session.commit()

                bought_count = sum(1 for t in tranches if t.status == TrancheStatus.BOUGHT.value)
                result["tickers"].append({
                    "ticker": ticker_obj.ticker, "synced": True,
                    "bought_tranches": bought_count,
                    "orders_submitted": len(orders),
                })
            except Exception as e:
                logger.exception(f"  [{ticker_obj.ticker}] 처리 오류: {e}")
                _log_structured(session, "ERROR", ticker_obj.ticker, "에러", f"종목 처리 오류: {e}")
                result["errors"].append(f"{ticker_obj.ticker}: {str(e)}")

        next_run = get_next_worker_run_kst()
        _log(f"다음 워커 실행: {next_run}")
        msg_done = f"워커 실행 완료: {len(result['tickers'])}종목 처리"
        _log(msg_done)
        logger.info(f"종사종팔 {msg_done}")
    except Exception as e:
        logger.exception(f"워커 실행 오류: {e}")
        result["errors"].append(str(e))
        try:
            _log(f"워커 실행 오류: {e}", "ERROR")
        except Exception:
            pass
    finally:
        session.close()

    return result
