# -*- coding: utf-8 -*-
"""
무한매수법 V2.2 - 워커 (스케줄 실행 엔진)
미국장 개시 30분 후: 잔고/체결 동기화 → 주문 생성 → API 제출
"""
import logging
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL, KILL_SWITCH_FILE, TRADING_MODE, CTAC_TLNO, KIS_DEVL_YAML
from .kis_client import KISClient, get_shared_client, ORD_DVSN_LIMIT, ORD_DVSN_LOC, ORD_DVSN_MOC
from .models import Order, Portfolio, PortfolioState, AppLog, Trade, SystemConfig, CycleHistory
from .settings_store import get_kis_settings, save_account_summary
from .trading_logic import (
    generate_orders,
    orders_hash,
    sync_state_from_api,
    extract_account_summary,
    calc_T_from_avg,
    calc_star_pct,
)


logger = logging.getLogger(__name__)
_worker_lock = threading.Lock()


# ========== 안전장치 ==========
def is_kill_switch_on() -> bool:
    return Path(KILL_SWITCH_FILE).exists()


def is_us_trading_day() -> bool:
    """미국 거래일 여부 - 미국 동부(ET) 시간 기준 월~금"""
    try:
        from zoneinfo import ZoneInfo
        et = datetime.now(ZoneInfo("America/New_York"))
        return et.weekday() < 5
    except Exception:
        try:
            import pytz
            et = datetime.now(pytz.timezone("America/New_York"))
            return et.weekday() < 5
        except Exception:
            return datetime.utcnow().weekday() < 5


def get_us_market_run_time_kst() -> tuple:
    """미국장 개시 30분 후(10:00 ET)를 KST로 변환하여 (hour, minute) 반환"""
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
        target_et = et_now.replace(hour=10, minute=0, second=0, microsecond=0)
        kst = target_et.astimezone(ZoneInfo("Asia/Seoul"))
        return kst.hour, kst.minute
    except Exception:
        try:
            import pytz
            et_tz = pytz.timezone("America/New_York")
            et_now = datetime.now(et_tz)
            target_et = et_now.replace(hour=10, minute=0, second=0, microsecond=0)
            kst_tz = pytz.timezone("Asia/Seoul")
            kst = target_et.astimezone(kst_tz)
            return kst.hour, kst.minute
        except Exception:
            return 23, 0


def get_next_worker_run_kst() -> str:
    """다음 워커 실행 예정 시각 (KST) 문자열. 무한매수법은 run_h~run_h+5 매시 실행"""
    run_h, run_m = get_us_market_run_time_kst()
    try:
        from zoneinfo import ZoneInfo
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    except Exception:
        try:
            import pytz
            now_kst = datetime.now(pytz.timezone("Asia/Seoul"))
        except Exception:
            now_kst = datetime.now()
    next_run = now_kst.replace(hour=run_h, minute=run_m, second=0, microsecond=0)
    if next_run <= now_kst:
        next_run += timedelta(days=1)
    return next_run.strftime("%m/%d %H:%M") + " KST"


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


# ========== DB 헬퍼 ==========
def get_portfolio_state(session: Session, portfolio_id: int) -> Optional[PortfolioState]:
    stmt = select(PortfolioState).where(
        PortfolioState.portfolio_id == portfolio_id
    ).order_by(PortfolioState.synced_at.desc()).limit(1)
    return session.scalar(stmt)


def ensure_portfolio_state(session: Session, portfolio: Portfolio) -> PortfolioState:
    state = get_portfolio_state(session, portfolio.id)
    if state is None:
        state = PortfolioState(portfolio_id=portfolio.id)
        session.add(state)
        session.commit()
    return state


def _ts_kst() -> str:
    """KST 기준 시각 문자열 (HH:MM:SS)"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S")
    except Exception:
        return datetime.now().strftime("%H:%M:%S")


def log_app(session: Session, level: str, message: str, portfolio_id: Optional[int] = None):
    session.add(AppLog(portfolio_id=portfolio_id, level=level, message=message))
    session.commit()


def log_structured(session: Session, level: str, ticker: str, category: str, detail: str,
                  portfolio_id: Optional[int] = None):
    """구조화 로그: [HH:MM:SS] [TICKER] [카테고리] 상세 (종목별·시간별 구분)"""
    msg = f"[{_ts_kst()}] [{ticker}] [{category}] {detail}"
    log_app(session, level, msg, portfolio_id)


def record_trade(session: Session, portfolio_id: int, order_id: Optional[int],
                 trade_date: str, side: str, order_type: str,
                 price: float, qty: int, amount: float, odno: Optional[str] = None,
                 order_price: Optional[float] = None, buy_seq: Optional[str] = None):
    """체결된 거래를 trades 테이블에 기록 (price=실제 체결가, amount=체결금액=체결가×수량)"""
    trade = Trade(
        portfolio_id=portfolio_id,
        order_id=order_id,
        trade_date=trade_date,
        side=side,
        order_type=order_type,
        price=price,
        order_price=order_price,
        qty=qty,
        amount=amount,
        odno=odno,
        buy_seq=buy_seq,
    )
    session.add(trade)
    session.commit()
    return trade


def _cycle_trade_filter(portfolio: Portfolio) -> tuple[str | None, int | None]:
    """
    현재 싸이클의 Trade 집계 경계.
    - cycle_start_trade_id가 있으면 Trade.id >= that
    - 없으면 cycle_start_date 기반 (YYYYMMDD)
    """
    stid = getattr(portfolio, "cycle_start_trade_id", None)
    if stid:
        return None, int(stid)
    sdate = getattr(portfolio, "cycle_start_date", None)
    return (str(sdate).strip() if sdate else None), None


# ========== 메인 워커 ==========
def run_worker_once(ctac_tlno: str = CTAC_TLNO, force: bool = False) -> dict:
    """
    워커 1회 실행
    force=True: 모든 제약 무시 (강제 동기화 버튼) — 잔고 동기화만 수행, 주문 제출 안 함
    force=False: 정규 실행 — 거래일 확인 후 동기화 + 주문 제출
    """
    # 정규 실행(force=False)은 잠깐 대기 후 실행해 스케줄 충돌로 인한 주문 누락을 줄인다.
    # 동기화 실행(force=True)은 기존처럼 즉시 실패 처리해 큐가 밀리지 않게 한다.
    if force:
        acquired = _worker_lock.acquire(blocking=False)
    else:
        # 정규 주문 워커는 동기화 잡(force=True)보다 우선되어야 하므로 대기 시간을 충분히 준다.
        acquired = _worker_lock.acquire(timeout=90)
    if not acquired:
        logger.info("무한매수법 워커: 다른 작업 실행 중 - 건너뜀")
        return {"success": False, "portfolios": [], "errors": ["다른 작업 실행 중 - 건너뜀"]}
    try:
        return _run_worker_once_impl(ctac_tlno, force)
    finally:
        _worker_lock.release()


def _run_worker_once_impl(ctac_tlno: str = CTAC_TLNO, force: bool = False) -> dict:
    """워커 실제 로직 (락 획득 후 호출)"""
    logger.info("=" * 60)
    logger.info("무한매수법 워커 실행 시작" + (" (동기화만)" if force else ""))

    result = {"success": False, "portfolios": [], "errors": []}
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()

    quiet = force  # 2분 동기화(force=True) 시 INFO 로그 생략

    def _log(msg: str, level: str = "INFO"):
        if quiet and level == "INFO":
            return
        log_app(session, level, msg)
        session.commit()

    if not quiet:
        _log("워커 실행 시작 (동기화 + 주문 제출)")

    if is_kill_switch_on():
        _log("다음 할 일: 대기 (Kill Switch ON - 거래 중단)")
        result["errors"].append("Kill Switch 활성화됨 - 실행 중지")
        _log("워커 실행 스킵 (Kill Switch)")
        logger.info("무한매수법 워커 실행 스킵 (Kill Switch)")
        session.close()
        return result

    is_trading_day = is_us_trading_day()
    if not force and not is_trading_day:
        _log("다음 할 일: 다음 거래일까지 대기 (미거래일)")
        result["errors"].append("미국 거래일 아님 - 실행 스킵")
        result["success"] = True
        _log("워커 실행 스킵 (미거래일)")
        logger.info("무한매수법 워커 실행 스킵 (미거래일)")
        session.close()
        return result

    try:
        cfg = get_kis_settings(DATABASE_URL)
    except Exception:
        cfg = None

    if cfg:
        trading_mode = cfg.get("trading_mode") or TRADING_MODE
        ctac_tlno = cfg.get("ctac_tlno") or ctac_tlno
        client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
    else:
        client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)

    today = _today_kst()

    try:
        if not client.auth(ctac_tlno):
            _log("KIS API 인증: 실패 ✗", "ERROR")
            result["errors"].append("KIS API 인증 실패 - 대시보드 설정에서 앱키/시크릿을 확인하세요")
            _log("워커 실행 완료: 0포트폴리오 (인증 실패)", "ERROR")
            logger.info("무한매수법 워커 실행 완료: 0포트폴리오 (인증 실패)")
            return result

        _log("KIS API 인증: 성공 ✓")

        skip_orders = force

        portfolios = session.scalars(
            select(Portfolio).where(Portfolio.is_active == True, Portfolio.trading_enabled == True)
        ).all()
        if not portfolios:
            _log("다음 할 일: 포트폴리오 추가 후 재실행")
            result["errors"].append("활성화된 포트폴리오가 없습니다")
            result["success"] = True
            _log("워커 실행 완료: 0포트폴리오")
            logger.info("무한매수법 워커 실행 완료: 0포트폴리오")
            return result

        # 체결기준잔고 1회만 조회 (계좌 단위, 포트폴리오별 중복 호출 제거)
        try:
            pb_cache = client.inquire_present_balance(ctac_tlno=ctac_tlno)
            _log("체결기준잔고 API: 성공 ✓" if pb_cache else "체결기준잔고 API: 응답 없음", "WARNING" if not pb_cache else "INFO")
        except Exception as e:
            pb_cache = {}
            _log(f"체결기준잔고 API: 실패 ✗ ({e})", "WARNING")

        for pf in portfolios:
            try:
                pf_result = _process_portfolio(session, client, pf, today, ctac_tlno,
                                               skip_orders=skip_orders, pb_cache=pb_cache,
                                               quiet=quiet)
                result["portfolios"].append(pf_result)
            except Exception as e:
                logger.exception(f"포트폴리오 {pf.ticker} 처리 오류: {e}")
                log_app(session, "ERROR", str(e), pf.id)
                result["errors"].append(f"{pf.ticker}: {e}")

        result["success"] = True
        _log(f"다음 워커 실행: {get_next_worker_run_kst()}")
        # 포트폴리오별 주문 결과 요약 (제출O/제출X/스킵이유)
        def _order_summary(pr: dict) -> str:
            ticker = pr.get("ticker", "?")
            ords = pr.get("orders", [])
            if not ords:
                return f"{ticker}: 동기화만"
            if any(o.get("status") for o in ords):
                ok = sum(1 for o in ords if o.get("status") == "success")
                fail = sum(1 for o in ords if o.get("status") == "fail")
                if ok > 0 or fail > 0:
                    return f"{ticker}: 주문 제출 {ok}건✓{f', {fail}건실패' if fail else ''}"
            msg = (ords[0].get("msg") or "") if ords else ""
            if "최초 매수 주문 제출 완료" in msg:
                return f"{ticker}: 최초매수 제출 ✓"
            if "최초 매수 실패" in msg:
                return f"{ticker}: 최초매수 실패"
            if "미체결" in msg and "스킵" in msg:
                return f"{ticker}: 스킵(미체결 존재)"
            if "오늘 이미 주문 제출됨" in msg:
                return f"{ticker}: 스킵(오늘 이미 제출됨)"
            if "조건 미충족" in msg:
                return f"{ticker}: 스킵(조건 미충족)"
            if "동기화 완료" in msg:
                return f"{ticker}: 동기화만"
            return f"{ticker}: {msg[:40]}"
        summary_str = " | ".join(_order_summary(pr) for pr in result.get("portfolios", []))
        msg_done = f"워커 실행 완료: {len(result['portfolios'])}포트폴리오 | {summary_str}"
        _log(msg_done)
        logger.info(f"무한매수법 {msg_done}")
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


def _execute_initial_buy(
    session: Session,
    client: KISClient,
    portfolio: Portfolio,
    today: str,
    ctac_tlno: str,
) -> tuple[bool, str]:
    """최초 1회 매수 실행 (현재가+5% 지정가). 장 마감 시 전일종가 fallback."""
    B = portfolio.B
    if B <= 0:
        return False, "1회투자금(B)이 0 이하"
    ref_price = client.inquire_price(
        pdno=portfolio.ticker,
        ovrs_excg_cd=portfolio.ovrs_excg_cd,
        ctac_tlno=ctac_tlno,
    )
    price_src = "현재가"
    if ref_price <= 0:
        prev_close, src = client.inquire_prev_close(
            pdno=portfolio.ticker,
            ovrs_excg_cd=portfolio.ovrs_excg_cd,
            ctac_tlno=ctac_tlno,
        )
        if prev_close > 0:
            ref_price = prev_close
            price_src = "전일종가" if src == "base" else "기준가"
    if ref_price <= 0:
        daily_close = client.inquire_daily_price(
            pdno=portfolio.ticker,
            ovrs_excg_cd=portfolio.ovrs_excg_cd,
            ctac_tlno=ctac_tlno,
        )
        if daily_close > 0:
            ref_price = daily_close
            price_src = "일봉종가"
    if ref_price <= 0:
        # KIS 모두 실패 → yfinance 폴백 (무료, 인증 없음)
        try:
            import yfinance as yf
            t = yf.Ticker(portfolio.ticker)
            info = t.info
            p = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
            if p is not None:
                fp = float(p)
                if fp > 0:
                    ref_price = fp
                    price_src = "Yahoo(폴백)"
                    log_structured(session, "INFO", portfolio.ticker, "동기화", f"KIS 실패 → Yahoo 가격 ${ref_price:.2f} 사용", portfolio.id)
        except Exception as e:
            logger.debug(f"yfinance 폴백 실패 ({portfolio.ticker}): {e}")
    if ref_price <= 0:
        return False, f"가격 조회 불가 ({portfolio.ticker}). 현재가/전일종가/일봉/Yahoo 모두 실패."
    # 시장가 유사: 현재가면 +5%, fallback(전일종가/일봉/Yahoo)면 +10%로 체결 확률 확대
    pct_up = 1.05 if price_src == "현재가" else 1.10
    order_price = round(ref_price * pct_up, 2)
    qty = max(1, int(B / ref_price))
    res = client.order(
        ord_dv="buy",
        pdno=portfolio.ticker,
        ord_qty=str(qty),
        ovrs_ord_unpr=f"{order_price:.2f}",
        ord_dvsn=ORD_DVSN_LIMIT,
        ovrs_excg_cd=portfolio.ovrs_excg_cd,
        ctac_tlno=ctac_tlno,
    )
    status = "success" if res.get("rt_cd") == "0" else "fail"
    odno = res.get("output", {}).get("ODNO") if isinstance(res.get("output"), dict) else None
    ord_record = Order(
        portfolio_id=portfolio.id,
        order_date=today,
        side="buy",
        order_type="LIMIT",
        ord_dvsn=ORD_DVSN_LIMIT,
        price=order_price,
        qty=qty,
        amount=round(order_price * qty, 2),
        odno=odno,
        status=status,
        msg=res.get("msg1", ""),
    )
    session.add(ord_record)
    if status == "success":
        portfolio.initial_buy_done = True
        if not portfolio.cycle_start_date:
            portfolio.cycle_start_date = today
        pct_str = "5%" if price_src == "현재가" else "10%"
        log_structured(session, "INFO", portfolio.ticker, "주문제출",
                       f"최초매수 {qty}주 @ ${order_price:.2f} ({price_src} +{pct_str})", portfolio.id)
        return True, f"주문 완료 {qty}주 @ ${order_price:.2f}"
    return False, res.get("msg1", "주문 실패")


# ========== 최초 시장가 매수 ==========
def run_initial_buy(portfolio_id: int, ctac_tlno: str = CTAC_TLNO) -> dict:
    """
    포트폴리오 ON 시 최초 1회 시장가 매수
    현재가 조회 → +5% 높은 가격으로 지정가 매수 (사실상 시장가)
    체결 시 실제 체결가격·수량은 다음 동기화에서 반영
    """
    result = {"success": False, "message": ""}
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()

    try:
        pf = session.get(Portfolio, portfolio_id)
        if not pf:
            result["message"] = "포트폴리오 없음"
            return result

        cfg = get_kis_settings(DATABASE_URL)
        if cfg:
            trading_mode = cfg.get("trading_mode") or TRADING_MODE
            ctac_tlno = cfg.get("ctac_tlno") or ctac_tlno
            client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
        else:
            client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)

        if not client.auth(ctac_tlno):
            result["message"] = "KIS API 인증 실패"
            return result

        today = _today_kst()
        ok, msg = _execute_initial_buy(session, client, pf, today, ctac_tlno)
        session.commit()
        if ok:
            result["success"] = True
            result["message"] = f"{pf.ticker} 최초 매수 주문 완료. 체결가는 동기화 시 자동 반영됩니다."
        else:
            log_structured(session, "ERROR", pf.ticker, "주문실패", f"최초매수 실패: {msg}", pf.id)
            result["message"] = msg
    except Exception as e:
        result["message"] = str(e)
        logger.exception(f"최초 매수 오류: {e}")
    finally:
        session.close()

    return result


# ========== 포트폴리오 1건 처리 ==========
def _process_portfolio(
    session: Session,
    client: KISClient,
    portfolio: Portfolio,
    today: str,
    ctac_tlno: str,
    skip_orders: bool = False,
    pb_cache: dict = None,
    quiet: bool = False,
) -> dict:
    result = {"ticker": portfolio.ticker, "orders": [], "synced": False}
    state = ensure_portfolio_state(session, portfolio)

    def _plog(level: str, msg: str):
        """포트폴리오 로그. quiet 모드에서 INFO는 생략."""
        if quiet and level == "INFO":
            return
        log_app(session, level, msg, portfolio.id)

    # ----- 1) 동기화 -----
    try:
        df1, df2 = client.inquire_balance(
            ovrs_excg_cd=portfolio.ovrs_excg_cd,
            tr_crcy_cd="USD",
            ctac_tlno=ctac_tlno,
        )
        log_structured(session, "INFO", portfolio.ticker, "동기화", "잔고/체결조회 API 성공 ✓", portfolio.id)
        dt_start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
        dt_recent = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

        def _fetch_ccnl(start_dt: str, end_dt: str) -> pd.DataFrame:
            parts = []
            for ovrs in ["NASD", "NYSE", "AMEX"]:
                df = client.inquire_ccnl(
                    pdno=portfolio.ticker,
                    ord_strt_dt=start_dt,
                    ord_end_dt=end_dt,
                    sll_buy_dvsn="00",
                    ccld_nccs_dvsn="01",
                    ovrs_excg_cd=ovrs,
                    ctac_tlno=ctac_tlno,
                )
                if not df.empty:
                    parts.append(df)
            if not parts:
                return client.inquire_ccnl(
                    pdno=portfolio.ticker,
                    ord_strt_dt=start_dt,
                    ord_end_dt=end_dt,
                    sll_buy_dvsn="00",
                    ccld_nccs_dvsn="01",
                    ovrs_excg_cd="%",
                    ctac_tlno=ctac_tlno,
                )
            out = pd.concat(parts, ignore_index=True)
            if len(parts) > 1:
                odno_col = next((c for c in ["odno", "ODNO", "ORGN_ODNO"] if c in out.columns), None)
                if odno_col:
                    out = out.drop_duplicates(subset=[odno_col], keep="first")
            return out

        # 90일 전체 조회 (TQQQ 등 체결 많은 종목은 페이지 제한으로 최근 일부 누락 가능)
        ccnl_df = _fetch_ccnl(dt_start, today)
        # 최근 7일 추가 조회 후 병합 (최근 체결 누락 보정, keep='last'로 최근 데이터 우선)
        recent_df = _fetch_ccnl(dt_recent, today)
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
        trade_cutoff = _get_current_cycle_trade_cutoff_min_inclusive(session, portfolio)
        sync_state_from_api(
            portfolio, state, df1, df2, ccnl_df, trade_cutoff_date=trade_cutoff
        )
        state.synced_at = datetime.utcnow()
        session.commit()
        result["synced"] = True

        try:
            logger.info(f"[잔고 df1] cols={list(df1.columns) if not df1.empty else '비어있음'}, rows={len(df1)}")
            logger.info(f"[잔고 df2] cols={list(df2.columns) if not df2.empty else '비어있음'}, rows={len(df2)}")
            if not df1.empty:
                logger.info(f"[잔고 df1 데이터] {df1.iloc[0].to_dict()}")
            if not df2.empty:
                logger.info(f"[잔고 df2 데이터] {df2.iloc[0].to_dict()}")
            acct = extract_account_summary(df1, df2)
            # 체결기준현재잔고 (pb_cache 재사용, 중복 API 호출 방지)
            pb = pb_cache or {}
            acct["cash"] = pb.get("deposit_usd", 0.0)
            exrt = pb.get("exrt", 0.0)
            acct["exrt"] = exrt
            tot_krw = pb.get("tot_asst_krw", 0.0)
            if exrt > 0 and tot_krw > 0:
                acct["tot_asst_amt"] = round(tot_krw / exrt, 2)
            else:
                acct["tot_asst_amt"] = round(acct["stock_evlu"] + acct["cash"], 2)
            logger.info(f"[계좌요약] 계좌총액=${acct.get('tot_asst_amt',0)}, "
                        f"주식평가=${acct['stock_evlu']}, 예수금=${acct.get('cash',0)}, "
                        f"평가손익=${acct['pnl']}")
            if acct["stock_evlu"] > 0 or acct["buy_amt"] > 0 or acct["cash"] > 0:
                save_account_summary(acct, DATABASE_URL)
            else:
                logger.warning("주식평가/매입금액/예수금 모두 0 - API 응답에서 해당 필드를 찾지 못했습니다")
        except Exception as ex:
            logger.warning(f"계좌총액 저장 실패: {ex}")

        # 체결 내역 → trades 기록 (WebSocket 1차, REST는 누락 보정용)
        _record_executions(session, portfolio, today, ccnl_df)
        # order_price 비어 있는 Trade → Order 테이블에서 주문가 보정
        _backfill_order_prices(session, portfolio.id)
        # buy_seq 비어 있는 매수 Trade → 회차 번호 소급 적용
        _backfill_buy_seq(session, portfolio)
        # 누적매수/매도는 API 조회기간(예: 90일)에 영향받지 않도록 DB trades 기준으로 재계산
        _recalc_state_amounts_from_trades(session, portfolio, state)

        # ----- QUARTER → NORMAL 전환 감지 (LOC 매도 체결 확인) -----
        if state.mode == "QUARTER" and 1 <= state.quarter_step <= 10:
            _check_quarter_loc_sell(session, portfolio, state, today)

        # ----- 싸이클 종료 감지 -----
        cycle_ended = _check_cycle_end(session, portfolio, state, today)
        if cycle_ended:
            result["cycle_ended"] = True
            result["cycle_number"] = getattr(portfolio, "current_cycle", 1) - 1
            log_structured(session, "INFO", portfolio.ticker, "싸이클종료",
                          f"#{result['cycle_number']} 종료 → 다음 싸이클 시작", portfolio.id)

    except Exception as e:
        log_structured(session, "ERROR", portfolio.ticker, "에러", f"잔고/체결조회 API 실패: {e}", portfolio.id)
        log_structured(session, "ERROR", portfolio.ticker, "에러", f"동기화 실패: {e}", portfolio.id)
        raise

    if skip_orders:
        log_structured(session, "INFO", portfolio.ticker, "동기화", "강제동기화 (주문 제출 없음)", portfolio.id)
        result["orders"] = [{"msg": "동기화 완료 (주문 없음)"}]
        return result

    # NASD, NYSE, AMEX 모두 조회 (다른 거래소 미체결 누락 방지)
    nccs_parts = []
    for ovrs in ["NASD", "NYSE", "AMEX"]:
        df_part = client.inquire_nccs(ovrs_excg_cd=ovrs, ctac_tlno=ctac_tlno)
        if not df_part.empty:
            nccs_parts.append(df_part)
    nccs_df = pd.concat(nccs_parts, ignore_index=True) if nccs_parts else client.inquire_nccs(
        ovrs_excg_cd=portfolio.ovrs_excg_cd, ctac_tlno=ctac_tlno
    )
    ticker_pending = False
    if not nccs_df.empty:
        pdno_col = next((c for c in nccs_df.columns if str(c).upper().replace("_", "") == "PDNO"), "pdno")
        ticker_rows = nccs_df[nccs_df[pdno_col].astype(str).str.strip() == portfolio.ticker]
        ticker_pending = not ticker_rows.empty

    # ----- 보유 0 + 최초매수 미완료 → 자동 최초 1회 매수 (장개시 후 워커 실행 시) -----
    if not getattr(portfolio, "initial_buy_done", True):
        if state.qty > 0:
            portfolio.initial_buy_done = True
            session.commit()
            log_structured(session, "INFO", portfolio.ticker, "동기화", "이미 보유 중 - 최초매수 완료 처리", portfolio.id)
        elif ticker_pending:
            log_structured(session, "INFO", portfolio.ticker, "스킵", "미체결 대기 중 - 신규 주문 스킵", portfolio.id)
            log_structured(session, "INFO", portfolio.ticker, "미체결", "체결 대기", portfolio.id)
            msg = f"{portfolio.ticker} 미체결 매수 대기 중 - 신규 주문 스킵"
            result["orders"] = [{"msg": msg}]
            return result
        else:
            log_structured(session, "INFO", portfolio.ticker, "주문제출", "최초 1회 매수 → KIS API 호출", portfolio.id)
            ok, msg = _execute_initial_buy(session, client, portfolio, today, ctac_tlno)
            session.commit()
            if ok:
                log_structured(session, "INFO", portfolio.ticker, "주문제출", "최초매수 제출 성공 ✓", portfolio.id)
                log_structured(session, "INFO", portfolio.ticker, "미체결", "체결 대기 (동기화 시 반영)", portfolio.id)
                result["orders"] = [{"msg": f"최초 매수 주문 제출 완료 ({msg})"}]
                state.last_run_date = today
                session.commit()
            else:
                log_structured(session, "ERROR", portfolio.ticker, "주문실패", f"최초매수 실패: {msg}", portfolio.id)
                log_structured(session, "INFO", portfolio.ticker, "에러", "다음 워커에서 재시도", portfolio.id)
                result["orders"] = [{"msg": f"최초 매수 실패: {msg}"}]
            return result

    # ----- 2) 주문 중복 방지: KIS 미체결 있으면 스킵 -----
    if ticker_pending:
        n_pending = len(ticker_rows) if not ticker_rows.empty else 0
        detail = f"KIS 미체결 {n_pending}건 존재 → 신규 주문 스킵 (체결 대기 또는 HTS에서 취소 후 재시도)"
        log_structured(session, "INFO", portfolio.ticker, "스킵", detail, portfolio.id)
        log_structured(session, "INFO", portfolio.ticker, "미체결", "체결 대기중", portfolio.id)
        result["orders"] = [{"msg": f"{portfolio.ticker} {detail}"}]
        state.last_run_date = today
        session.commit()
        return result

    # KIS에 미체결 없으면 DB 기록과 무관하게 진행 (과거 success Order = 체결/취소 완료된 것)

    # ----- 3) 주문 생성 -----
    orders = generate_orders(portfolio, state, today)

    if not orders:
        detail = (
            f"오늘 주문 없음 (조건 미충족) "
            f"[qty={state.qty}, avg=${state.avg_price:.2f}, T={state.T:.1f}, "
            f"mode={state.mode}, initial_buy_done={getattr(portfolio, 'initial_buy_done', True)}]"
        )
        log_structured(session, "INFO", portfolio.ticker, "스킵", detail, portfolio.id)
        result["orders"] = [{"msg": f"{portfolio.ticker} 오늘 주문 없음 (조건 미충족)"}]
        state.last_run_date = today
        session.commit()
        return result

    order_desc = ", ".join(f"{'매수' if item.side == 'buy' else '매도'} {item.order_type}" for item in orders)
    log_structured(session, "INFO", portfolio.ticker, "주문제출", f"예정: {order_desc} → API 호출", portfolio.id)

    # ----- 4) 주문 제출 -----
    ok_count, fail_count = 0, 0
    prev_side = None
    for item in orders:
        if prev_side and prev_side != item.side:
            delay = random.uniform(15, 20)
            logger.info(f"[{portfolio.ticker}] 매도→매수 전환: {delay:.1f}초 대기 (cross 방지)")
            time.sleep(delay)
        ord_dvsn = ORD_DVSN_LIMIT
        if item.order_type == "MOC":
            ord_dvsn = ORD_DVSN_MOC
        elif item.order_type == "LOC":
            ord_dvsn = ORD_DVSN_LOC
        price_str = "0" if item.order_type == "MOC" else f"{item.price:.2f}"
        res = client.order(
            ord_dv=item.side,
            pdno=portfolio.ticker,
            ord_qty=str(item.qty),
            ovrs_ord_unpr=price_str,
            ord_dvsn=ord_dvsn,
            ovrs_excg_cd=portfolio.ovrs_excg_cd,
            ctac_tlno=ctac_tlno,
        )
        odno_val = res.get("output", {}).get("ODNO") if isinstance(res.get("output"), dict) else None
        status = "success" if res.get("rt_cd") == "0" else "fail"
        if status == "success":
            ok_count += 1
            side_kr = "매수" if item.side == "buy" else "매도"
            odno_display = odno_val or "(대기중)"
            log_structured(session, "INFO", portfolio.ticker, "주문제출",
                          f"{side_kr} {item.order_type} ${item.price:.2f} x {item.qty}주 → 제출성공 odno={odno_display}", portfolio.id)
        else:
            fail_count += 1

        ord_record = Order(
            portfolio_id=portfolio.id,
            order_date=today,
            side=item.side,
            order_type=item.order_type,
            ord_dvsn=ord_dvsn,
            price=item.price,
            qty=item.qty,
            amount=item.amount,
            odno=odno_val,
            status=status,
            msg=res.get("msg1", ""),
        )
        session.add(ord_record)
        session.flush()

        # 체결 내역은 동기화 시 inquire_ccnl(체결만)로 _record_executions에서만 기록 (주문 제출 시에는 기록하지 않음)
        result["orders"].append({
            "side": item.side,
            "type": item.order_type,
            "price": item.price,
            "qty": item.qty,
            "status": status,
            "msg": ord_record.msg,
        })
        if res.get("rt_cd") != "0":
            side_kr = "매수" if item.side == "buy" else "매도"
            msg1 = res.get("msg1", "") or "(오류메시지 없음)"
            msg_cd = res.get("msg_cd", "")
            detail = f"{side_kr} {item.order_type} ${item.price:.2f} x {item.qty}주 거부: {msg1}"
            if msg_cd:
                detail += f" (코드={msg_cd})"
            logger.error(f"[{portfolio.ticker}] {detail} | KIS: {res}")
            log_structured(session, "ERROR", portfolio.ticker, "주문실패", detail, portfolio.id)

        prev_side = item.side
        time.sleep(0.5)

    if fail_count > 0:
        log_structured(session, "WARNING", portfolio.ticker, "주문제출",
                       f"결과: {ok_count}건 성공, {fail_count}건 실패 (미체결 예정)", portfolio.id)
    else:
        log_structured(session, "INFO", portfolio.ticker, "주문제출", f"총 {ok_count}건 제출 성공 ✓", portfolio.id)
    log_structured(session, "INFO", portfolio.ticker, "미체결", f"{ok_count}건 체결 대기중 (장중 LOC/MOC)", portfolio.id)

    # ----- 5) QUARTER 모드 step 관리 -----
    if state.mode == "QUARTER" and orders:
        if state.quarter_step == 0:
            # MOC 1/4 매도 주문 제출 완료 → step 1로, 10회 분할매수금 재계산
            state.quarter_step = 1
            if state.cash > 0:
                state.quarter_base_cash = min(portfolio.B, state.cash / 10)
            logger.info(f"[{portfolio.ticker}] QUARTER: MOC 매도 주문 완료, "
                        f"10회 분할매수금=${state.quarter_base_cash:.2f}, step→1")
        elif 1 <= state.quarter_step <= 10:
            state.quarter_step += 1
            logger.info(f"[{portfolio.ticker}] QUARTER: step {state.quarter_step - 1}→{state.quarter_step}")
            if state.quarter_step > 10:
                # 10회 완료 → step 0으로 리셋 (다음에 MOC 1/4 매도)
                state.quarter_step = 0
                logger.info(f"[{portfolio.ticker}] QUARTER: 10회 완료 → MOC 1/4 매도 예정")

    state.last_run_date = today
    state.last_orders_hash = orders_hash(orders, portfolio.ticker, today)
    session.commit()
    return result


def _check_quarter_loc_sell(session: Session, portfolio: Portfolio,
                            state: PortfolioState, today: str):
    """
    QUARTER 모드 step 1~10 중 LOC 매도가 체결되었는지 확인.
    체결 확인 시 → NORMAL(후반전)으로 복귀.
    """
    from sqlalchemy import and_, func
    cycle_start = getattr(portfolio, "cycle_start_date", None) or "19000101"
    loc_sell_count = session.scalar(
        select(func.count(Trade.id))
        .where(and_(
            Trade.portfolio_id == portfolio.id,
            Trade.side == "sell",
            Trade.order_type == "LOC",
            Trade.trade_date >= cycle_start,
        ))
    ) or 0
    if loc_sell_count > 0:
        logger.info(f"[{portfolio.ticker}] QUARTER 모드에서 LOC 매도 체결 감지 "
                    f"({loc_sell_count}건) → NORMAL(후반전) 복귀")
        state.mode = "NORMAL"
        state.quarter_step = 0
        state.quarter_base_cash = 0.0


def _check_cycle_end(session: Session, portfolio: Portfolio,
                     state: PortfolioState, today: str) -> bool:
    """
    싸이클 종료 감지: initial_buy_done=True이고 qty가 0이 되면 싸이클 종료.
    종료 시: 이력 기록 → 상태 초기화 → 다음 싸이클 준비
    """
    if not getattr(portfolio, "initial_buy_done", False):
        return False
    # qty=0이면서 "매도 체결이 있었던" 경우만 싸이클 종료로 본다.
    # (API/동기화 순간 누락 등으로 qty가 0으로 보이는 경우 매도 없이 싸이클이 올라가는 것을 방지)
    if state.qty > 0 or state.cum_buy_amount <= 0:
        return False

    cycle_num = getattr(portfolio, "current_cycle", 1) or 1
    # 이미 기록된 싸이클이면 중복 방지 (동기화만/전체실행 여러 번 호출 시)
    existing = session.scalar(
        select(CycleHistory).where(
            CycleHistory.portfolio_id == portfolio.id,
            CycleHistory.cycle_number == cycle_num,
        )
    )
    if existing:
        return False

    start_date = getattr(portfolio, "cycle_start_date", None) or today
    cutoff_date, cutoff_trade_id = _cycle_trade_filter(portfolio)

    # trades 테이블에서 현재 싸이클의 매수/매도 금액 집계
    from sqlalchemy import func
    buy_q = select(func.coalesce(func.sum(Trade.amount), 0.0)).where(
        Trade.portfolio_id == portfolio.id,
        Trade.side == "buy",
    )
    sell_q = select(func.coalesce(func.sum(Trade.amount), 0.0)).where(
        Trade.portfolio_id == portfolio.id,
        Trade.side == "sell",
    )
    if cutoff_trade_id is not None:
        buy_q = buy_q.where(Trade.id >= cutoff_trade_id)
        sell_q = sell_q.where(Trade.id >= cutoff_trade_id)
    elif cutoff_date:
        buy_q = buy_q.where(Trade.trade_date >= cutoff_date)
        sell_q = sell_q.where(Trade.trade_date >= cutoff_date)
    else:
        buy_q = buy_q.where(Trade.trade_date >= start_date)
        sell_q = sell_q.where(Trade.trade_date >= start_date)
    buy_sum = session.scalar(buy_q) or 0.0
    sell_sum = session.scalar(sell_q) or 0.0

    if sell_sum <= 0:
        return False

    # 안전장치: 현재 싸이클의 "마지막 체결"이 매도가 아닐 때는 종료하지 않는다.
    # (잔고 API 순간값/동기화 지연으로 qty=0으로 보이는 경우, 다음 매수 회차가 '최초'로
    #  잘못 리셋되는 현상을 방지)
    last_trade_q = (
        select(Trade.side, Trade.trade_date, Trade.id)
        .where(Trade.portfolio_id == portfolio.id)
    )
    if cutoff_trade_id is not None:
        last_trade_q = last_trade_q.where(Trade.id >= cutoff_trade_id)
    elif cutoff_date:
        last_trade_q = last_trade_q.where(Trade.trade_date >= cutoff_date)
    else:
        last_trade_q = last_trade_q.where(Trade.trade_date >= start_date)
    last_trade = session.execute(
        last_trade_q.order_by(Trade.trade_date.desc(), Trade.id.desc()).limit(1)
    ).first()
    if not last_trade or (last_trade[0] != "sell"):
        return False

    # 기보유 매입금액(initial_holdings_cost)은 #1 싸이클의 "총매수"에 포함되어야
    # 수익이 과대계상되지 않음 (대시보드 T 계산과 동일한 기준).
    init_cost = float(getattr(portfolio, "initial_holdings_cost", 0) or 0)
    buy_total = buy_sum + (init_cost if cycle_num == 1 and init_cost > 0 else 0.0)

    profit = sell_sum - buy_total
    profit_pct = (profit / buy_total * 100) if buy_total > 0 else 0.0

    # 싸이클 이력 저장
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

    logger.info(f"[싸이클 종료] {portfolio.ticker} #{cycle_num}: "
                f"매수 ${buy_total:.2f}, 매도 ${sell_sum:.2f}, "
                f"수익 ${profit:.2f} ({profit_pct:.2f}%)")

    # 상태 초기화
    state.avg_price = 0.0
    state.qty = 0
    state.cum_buy_amount = 0.0
    state.cum_sell_amount = 0.0
    state.T = 0.0
    state.star_pct = getattr(portfolio, "R", 10.0) or 10.0
    state.mode = "NORMAL"
    state.quarter_step = 0
    state.quarter_base_cash = 0.0
    state.last_run_date = None
    state.last_orders_hash = None

    # 포트폴리오: 다음 싸이클 준비
    portfolio.current_cycle = cycle_num + 1
    portfolio.cycle_start_date = None
    portfolio.cycle_start_trade_id = None
    portfolio.initial_buy_done = False

    session.commit()
    return True


def _normalize_odno(s: str) -> str:
    """주문번호 정규화 (앞 0 제거) - KIS odno/ODNO/ORGN_ODNO 형식 차이 대응"""
    t = str(s or "").strip().lstrip("0")
    return t if t else "0"


def _find_order_by_odno(session: Session, portfolio_id: int, odno_raw: str, odno_norm: str):
    """Order.odno 조회 (형식 차이: 30224020 vs 030224020 등)"""
    ord_row = session.scalar(
        select(Order).where(Order.odno == odno_raw, Order.portfolio_id == portfolio_id)
    )
    if ord_row:
        return ord_row
    for o in session.scalars(
        select(Order).where(Order.portfolio_id == portfolio_id, Order.odno.isnot(None), Order.odno != "")
    ):
        if _normalize_odno(o.odno) == odno_norm:
            return o
    return None


def _row_val(row, *keys, default=None):
    """KIS API 응답 키 대소문자 차이 대응 (ccld_unpr / CCLD_UNPR 등)"""
    for k in keys:
        v = row.get(k) if hasattr(row, "get") else getattr(row, k, None)
        if v is not None and str(v).strip() and str(v) != "0":
            return v
        v2 = row.get(k.upper()) if hasattr(row, "get") else getattr(row, k.upper(), None)
        if v2 is not None and str(v2).strip() and str(v2) != "0":
            return v2
    return default


def _record_executions(session: Session, portfolio: Portfolio, today: str, ccnl_df):
    """체결 내역(ccnl_df) → trades 기록. 신규 건만. 기존(WebSocket)은 덮어쓰지 않음.
    price=체결가(ccld_unpr), order_price=주문가(ord_unpr), amount=체결가×수량
    포트폴리오 등록일(created_at) 이전 체결은 무시."""
    try:
        if ccnl_df is None or ccnl_df.empty:
            log_structured(session, "INFO", portfolio.ticker, "체결조회", "조회결과 0건 (API 빈 응답)", portfolio.id)
            return
        log_structured(session, "INFO", portfolio.ticker, "체결조회", f"조회 {len(ccnl_df)}건", portfolio.id)

        pf_created = getattr(portfolio, "created_at", None)
        pf_created_str = pf_created.strftime("%Y%m%d") if pf_created else None
        new_recorded = 0

        for _, row in ccnl_df.iterrows():
            odno_raw = str(_row_val(row, "odno", "ORGN_ODNO", "ODNO") or "").strip()
            if not odno_raw:
                continue
            odno_norm = _normalize_odno(odno_raw)

            # 포트폴리오 등록 전 체결은 기록하지 않음
            if pf_created_str:
                trade_date_raw = str(_row_val(row, "ord_dt") or "")
                if trade_date_raw and trade_date_raw < pf_created_str:
                    continue

            # REST에서 주문가 추출 (KIS 필드: ord_unpr, ovrs_ord_unpr, ord_prpr, ft_ord_unpr 등)
            order_price_val = None
            op = _row_val(row, "ord_unpr", "ovrs_ord_unpr", "ord_prpr", "ft_ord_unpr")
            if op is not None and str(op).strip():
                try:
                    order_price_val = float(op)
                except (TypeError, ValueError):
                    pass

            # 기존 Trade 조회 (odno 형식 차이: "30224020" vs "030224020" 등)
            existing = session.scalar(
                select(Trade).where(Trade.odno == odno_raw, Trade.portfolio_id == portfolio.id)
            )
            if not existing:
                for t in session.scalars(
                    select(Trade).where(
                        Trade.portfolio_id == portfolio.id,
                        Trade.odno.isnot(None),
                        Trade.odno != "",
                    )
                ):
                    if _normalize_odno(t.odno) == odno_norm:
                        existing = t
                        break
            if existing:
                # WebSocket으로 기록된 건: order_price 보정 (REST → 없으면 Order 테이블 조회)
                if existing.order_price is None:
                    val = order_price_val
                    if val is None:
                        ord_row = _find_order_by_odno(session, portfolio.id, odno_raw, odno_norm)
                        val = float(ord_row.price) if ord_row and ord_row.price else None
                    if val is not None:
                        existing.order_price = val
                        session.commit()
                continue

            # 체결가: ccld_unpr (실제 체결단가). 주문가: ord_unpr (별도 저장)
            price = float(_row_val(row, "ccld_unpr", "ft_ccld_unpr3") or 0)
            qty = int(float(_row_val(row, "ft_ccld_qty", "ccld_qty") or 0))
            amount = round(price * qty, 2) if price > 0 and qty > 0 else 0.0
            trade_date = str(_row_val(row, "ord_dt") or today)

            side_code = str(_row_val(row, "sll_buy_dvsn_cd") or "")
            side = "buy" if side_code in ("02", "2") else "sell"

            # 주문유형: Order 테이블에서 먼저 조회 (ccnl API에 ord_dvsn_cd 미포함 시 LIMIT 오판 방지)
            order_type = None
            ord_row = _find_order_by_odno(session, portfolio.id, odno_raw, odno_norm)
            if ord_row and ord_row.order_type:
                order_type = ord_row.order_type
            if not order_type:
                ord_dvsn = str(_row_val(row, "ord_dvsn_cd", "rvse_cncl_dvsn_cd") or "")
                ORD_TYPE_MAP = {"00": "LIMIT", "32": "LOO", "33": "MOC", "34": "LOC"}
                order_type = ORD_TYPE_MAP.get(ord_dvsn, "LIMIT")

            # buy_seq 계산: 매수 건에만 회차 부여
            buy_seq = None
            if side == "buy" and qty > 0:
                cutoff_date, cutoff_trade_id = _cycle_trade_filter(portfolio)
                q = select(func.count(Trade.id)).where(
                    Trade.portfolio_id == portfolio.id,
                    Trade.side == "buy",
                )
                if cutoff_trade_id is not None:
                    q = q.where(Trade.id >= cutoff_trade_id)
                elif cutoff_date:
                    q = q.where(Trade.trade_date >= cutoff_date)
                existing_buy_count = session.scalar(q) or 0
                buy_seq = "최초" if existing_buy_count == 0 else str(existing_buy_count + 1)

            if qty > 0:
                tr = record_trade(
                    session,
                    portfolio.id,
                    None,
                    trade_date,
                    side,
                    order_type,
                    price,
                    qty,
                    amount,
                    odno_raw,
                    order_price=order_price_val,
                    buy_seq=buy_seq,
                )
                # 새 싸이클의 첫 매수 체결이면: 싸이클 시작 경계를 Trade.id로 고정
                if (
                    side == "buy"
                    and getattr(portfolio, "cycle_start_trade_id", None) in (None, 0, "")
                    and getattr(portfolio, "cycle_start_date", None) is None
                ):
                    portfolio.cycle_start_date = trade_date
                    portfolio.cycle_start_trade_id = tr.id
                    portfolio.initial_buy_done = True
                    session.commit()
                new_recorded += 1
                side_kr = "매수" if side == "buy" else "매도"
                log_structured(session, "INFO", portfolio.ticker, "주문체결",
                              f"{side_kr} {order_type} {qty}주 @ ${price:.2f} (odno={odno_raw})", portfolio.id)
        if new_recorded > 0:
            log_structured(session, "INFO", portfolio.ticker, "체결조회", f"신규 기록 {new_recorded}건", portfolio.id)
    except Exception as e:
        logger.debug(f"체결 기록 실패 (무시): {e}")


def _recalc_state_amounts_from_trades(session: Session, portfolio: Portfolio, state: PortfolioState) -> None:
    """현재 싸이클 구간의 trades 합계로 state.cum_* 재계산.

    목적: ccnl 조회기간(90일)이 지나도 누적매수/매도가 줄어들지 않도록 보정.
    이전 싸이클이 끝난 뒤에는 **종료일 다음날**부터만 합산해 매수누적/매도누적/순투입이 새 싸이클 기준으로 맞는다.
    """
    try:
        cutoff_date, cutoff_trade_id = _cycle_trade_filter(portfolio)
        q = select(Trade).where(Trade.portfolio_id == portfolio.id)
        if cutoff_trade_id is not None:
            q = q.where(Trade.id >= cutoff_trade_id)
        elif cutoff_date:
            q = q.where(Trade.trade_date >= cutoff_date)
        rows = session.scalars(q).all()
        init_cost = float(getattr(portfolio, "initial_holdings_cost", 0) or 0)
        if not rows:
            state.cum_buy_amount = round(init_cost, 2)
            state.cum_sell_amount = 0.0
            state.T = calc_T_from_avg(state.avg_price, state.qty, portfolio.B)
            target_r = getattr(portfolio, "R", 10.0) or 10.0
            state.star_pct = calc_star_pct(state.T, portfolio.A, target_r)
            session.commit()
            logger.info(
                f"[{portfolio.ticker}] 누적 재계산(trades): 체결 없음 → "
                f"buy=${state.cum_buy_amount:.2f}, sell=0, T={state.T:.1f}"
            )
            return

        buy_cum = 0.0
        sell_cum = 0.0
        for t in rows:
            amt = float(t.amount or 0)
            if amt <= 0 and t.price and t.qty:
                amt = round(float(t.price) * int(t.qty), 2)
            if t.side == "buy":
                buy_cum += amt
            elif t.side == "sell":
                sell_cum += amt

        state.cum_buy_amount = round(init_cost + buy_cum, 2)
        state.cum_sell_amount = round(sell_cum, 2)
        state.T = calc_T_from_avg(state.avg_price, state.qty, portfolio.B)
        target_r = getattr(portfolio, "R", 10.0) or 10.0
        state.star_pct = calc_star_pct(state.T, portfolio.A, target_r)
        session.commit()
        logger.info(
            f"[{portfolio.ticker}] 누적 재계산(trades): "
            f"buy=${state.cum_buy_amount:.2f}, sell=${state.cum_sell_amount:.2f}, T={state.T:.1f}"
        )
    except Exception as e:
        logger.debug(f"누적 재계산 실패(무시): {e}")


def _backfill_order_prices(session: Session, portfolio_id: int) -> None:
    """order_price가 None인 Trade, order_type이 LIMIT으로 잘못된 Trade → Order 테이블에서 보정"""
    try:
        trades = session.scalars(
            select(Trade).where(
                Trade.portfolio_id == portfolio_id,
                Trade.odno.isnot(None),
                Trade.odno != "",
            )
        ).all()
        dirty = False
        for t in trades:
            if not t.odno:
                continue
            needs_price = t.order_price is None
            needs_type = t.order_type == "LIMIT"
            if not needs_price and not needs_type:
                continue
            ord_row = session.scalar(
                select(Order).where(
                    Order.portfolio_id == portfolio_id,
                    Order.odno == t.odno,
                )
            )
            if not ord_row:
                continue
            if needs_price and ord_row.price:
                t.order_price = float(ord_row.price)
                dirty = True
            if needs_type and ord_row.order_type and ord_row.order_type != "LIMIT":
                t.order_type = ord_row.order_type
                dirty = True
        if dirty:
            session.commit()
    except Exception as e:
        logger.debug(f"order_price/order_type 보정 실패: {e}")


def _get_current_cycle_trade_cutoff_min_inclusive(session: Session, portfolio: Portfolio) -> str:
    """현재 싸이클에 속하는 Trade의 최소 trade_date (YYYYMMDD, 이상 조건에 사용).

    원칙: **cycle_start_date**(설정된 경우)를 최우선으로 사용한다.
    - 같은 날짜에 "전량매도(싸이클 종료)" 후 "재진입(새 싸이클 최초매수)"가 발생할 수 있어,
      종료일+1로 자르면 **동일 날짜의 새 싸이클 체결이 통째로 제외**되어 누적/회차가 꼬일 수 있음.

    cycle_start_date가 없으면:
    - 이전 싸이클 end_date(있으면) 또는 포트폴리오 등록일(created_at) 기준.
    """
    pf_created = getattr(portfolio, "created_at", None)
    pf_created_str = pf_created.strftime("%Y%m%d") if pf_created else None

    cycle_start = getattr(portfolio, "cycle_start_date", None)
    if cycle_start:
        if pf_created_str:
            return min(cycle_start, pf_created_str)
        return cycle_start

    last_cycle = session.scalar(
        select(CycleHistory)
        .where(CycleHistory.portfolio_id == portfolio.id)
        .order_by(CycleHistory.cycle_number.desc())
    )
    if last_cycle and last_cycle.end_date:
        # end_date는 해당 싸이클의 마지막 거래일(YYYYMMDD)만 알고, 하루 내 시각은 없으므로
        # 같은 날 재진입을 지원하려면 +1 하지 않는다.
        return last_cycle.end_date

    return pf_created_str or "19000101"


def _get_current_cycle_cutoff(session: Session, portfolio: Portfolio) -> str:
    """하위 호환: 현재 싸이클 trade 집계 하한(포함)."""
    return _get_current_cycle_trade_cutoff_min_inclusive(session, portfolio)


def _backfill_buy_seq(session: Session, portfolio: Portfolio) -> None:
    """buy_seq가 비어있는 매수 Trade에 회차 번호 소급 적용"""
    try:
        cutoff_date, cutoff_trade_id = _cycle_trade_filter(portfolio)
        q = select(Trade).where(
            Trade.portfolio_id == portfolio.id,
            Trade.side == "buy",
        )
        if cutoff_trade_id is not None:
            q = q.where(Trade.id >= cutoff_trade_id).order_by(Trade.id)
        elif cutoff_date:
            q = q.where(Trade.trade_date >= cutoff_date).order_by(Trade.trade_date, Trade.id)
        else:
            q = q.order_by(Trade.trade_date, Trade.id)
        buy_trades = session.scalars(q).all()
        dirty = False
        for idx, t in enumerate(buy_trades):
            expected_seq = "최초" if idx == 0 else str(idx + 1)
            if t.buy_seq != expected_seq:
                t.buy_seq = expected_seq
                dirty = True
        if dirty:
            session.commit()
    except Exception as e:
        logger.debug(f"buy_seq 보정 실패: {e}")


# ========== Kill Switch ==========
def kill_switch_activate():
    Path(KILL_SWITCH_FILE).touch()
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    cfg = get_kis_settings(DATABASE_URL)
    if cfg:
        trading_mode = cfg.get("trading_mode") or TRADING_MODE
        client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
    else:
        client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)
    try:
        client.auth(CTAC_TLNO)
        portfolios = session.scalars(select(Portfolio).where(Portfolio.is_active == True)).all()
        for pf in portfolios:
            nccs_df = client.inquire_nccs(ovrs_excg_cd=pf.ovrs_excg_cd, ctac_tlno=CTAC_TLNO)
            if not nccs_df.empty and "pdno" in nccs_df.columns:
                ticker_rows = nccs_df[nccs_df["pdno"] == pf.ticker]
                for _, row in ticker_rows.iterrows():
                    qty = row.get("nccs_qty") or row.get("ord_qty", 1)
                    client.order_cancel(
                        pdno=pf.ticker,
                        orgn_odno=str(row.get("odno", row.get("ORGN_ODNO", ""))),
                        ord_qty=str(int(float(qty) if qty else 1)),
                        ovrs_ord_unpr="0",
                        ovrs_excg_cd=pf.ovrs_excg_cd,
                        ctac_tlno=CTAC_TLNO,
                    )
    finally:
        session.close()


def kill_switch_deactivate():
    if Path(KILL_SWITCH_FILE).exists():
        Path(KILL_SWITCH_FILE).unlink()
