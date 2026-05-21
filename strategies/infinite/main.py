# -*- coding: utf-8 -*-
"""
무한매수법 V2.2 - FastAPI 서버 + 스케줄러
REST API, 대시보드, 매일 워커 실행 스케줄링
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    import pytz
    KST = pytz.timezone("Asia/Seoul")

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, select, desc, and_, func
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL, RUN_HOUR, RUN_MINUTE, TRADING_MODE, CTAC_TLNO, KIS_DEVL_YAML
from .kis_client import KISClient, get_shared_client, reset_shared_client
from .models import init_db, Portfolio, PortfolioState, Order, AppLog, Trade, CycleHistory
from .settings_store import get_settings_for_display, save_settings, get_account_summary, get_kis_settings
from .worker import (run_worker_once, kill_switch_activate, kill_switch_deactivate,
                    is_kill_switch_on, run_initial_buy, get_us_market_run_time_kst, get_next_worker_run_kst)
from .trading_logic import SUPPORTED_VERSIONS, generate_orders
from .trading_logic import calc_T, calc_star_pct

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# DB 엔진 및 세션
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ========== 앱 생명주기 ==========
def _pre_auth():
    """워커 실행 전 토큰 사전 발급 (1분 제한 회피)"""
    try:
        cfg = get_kis_settings(DATABASE_URL)
        if cfg:
            trading_mode = cfg.get("trading_mode") or TRADING_MODE
            ctac_tlno = cfg.get("ctac_tlno") or CTAC_TLNO
            client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
        else:
            client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)
            ctac_tlno = CTAC_TLNO
        if client.auth(ctac_tlno):
            logger.info("[사전인증] 토큰 발급 완료 - 워커 실행 대기 중")
        else:
            logger.warning("[사전인증] 토큰 발급 실패 - 워커 실행 시 재시도 예정")
    except Exception as e:
        logger.warning(f"[사전인증] 오류: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    서버 시작 시: DB 초기화, APScheduler로 매일 RUN_HOUR:RUN_MINUTE에 워커 실행
    워커 5분 전에 토큰 사전 발급하여 1분 제한 회피
    서버 종료 시: 스케줄러 정리
    """
    init_db(DATABASE_URL)
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    run_h, run_m = get_us_market_run_time_kst()

    # 워커 5분 전 사전 토큰 발급
    pre_m = run_m - 5
    pre_h = run_h
    if pre_m < 0:
        pre_m += 60
        pre_h = (pre_h - 1) % 24
    scheduler.add_job(
        _pre_auth,
        "cron",
        hour=pre_h,
        minute=pre_m,
        id="pre_auth",
    )

    # 워커: 00:00 KST + 매시 정각 6회 (00:00, 01:00, 02:00, 03:00, 04:00, 05:00)
    # 한 번 주문 들어가면/미체결 있으면 더 이상 주문 안 함
    for i in range(6):
        h = (run_h + i) % 24
        scheduler.add_job(
            run_worker_once,
            "cron",
            hour=h,
            minute=run_m,
            id=f"worker_{i}",
        )
    # 동기화 전용 잡은 정각(run_m)과 충돌하지 않게 run_m+1분부터 2분 간격으로 실행
    # 예: run_m=00 이면 01,03,05,... / run_m=30 이면 31,33,35,...
    sync_start_min = (run_m + 1) % 60
    sync_minutes = ",".join(str(m) for m in range(sync_start_min, 60, 2))
    scheduler.add_job(
        lambda: run_worker_once(CTAC_TLNO, force=True),
        "cron", minute=sync_minutes, second=20, id="sync_only",
        max_instances=1,
    )
    scheduler.start()
    logger.info(f"스케줄러 시작: 사전인증 {pre_h:02d}:{pre_m:02d} → 워커 {run_h:02d}:{run_m:02d}~{(run_h+5)%24:02d}:{run_m:02d} KST (매시)")
    try:
        from .kis_ws import start_ws_ccnl_thread
        start_ws_ccnl_thread()
    except Exception as e:
        logger.warning(f"WebSocket 체결통보 미시작: {e}")
    yield
    scheduler.shutdown()


app = FastAPI(title="무한매수법 V2.2", lifespan=lifespan)


# ========== Pydantic 스키마 ==========
class PortfolioCreate(BaseModel):
    """포트폴리오 등록용"""
    ticker: str
    strategy_version: str = "2.2"   # 무한매수법 버전: 2.2, 3.0
    seed: float
    A: int = 40
    R: float = 10.0
    fee_rate: float = 0.0
    ovrs_excg_cd: str = "NASD"
    already_holding: bool = False    # True: 이미 보유 중 (최초매수 건너뜀)
    initial_holdings_cost: float = 0.0  # 기존 보유분 매입금액 (T 계산 시 cum_buy에 가산, $)


class PortfolioResponse(BaseModel):
    id: int
    ticker: str
    seed: float
    A: int
    B: float
    mode: str
    T: float
    star_pct: float
    avg_price: float
    qty: int
    cash: float
    cum_buy_amount: float
    quarter_step: int
    last_run_date: str | None

    class Config:
        from_attributes = True


def get_state_for_portfolio(session: Session, portfolio: Portfolio) -> dict:
    """포트폴리오 최신 상태를 dict로 반환 (API 응답용)"""
    stmt = (
        select(PortfolioState)
        .where(PortfolioState.portfolio_id == portfolio.id)
        .order_by(PortfolioState.synced_at.desc())
        .limit(1)
    )
    state = session.scalar(stmt)
    if state is None:
        return {
            "mode": "NORMAL_전반전",
            "T": 0.0,
            "star_pct": getattr(portfolio, "R", 10.0) or 10.0,
            "avg_price": 0.0,
            "qty": 0,
            "cash": 0.0,
            "cum_buy_amount": 0.0,
            "cum_sell_amount": 0.0,
            "quarter_step": 0,
            "last_run_date": None,
        }
    mode_display = state.mode
    if state.mode == "NORMAL":
        mode_display = "NORMAL_전반전" if state.T < 20 else "NORMAL_후반전"
    elif state.mode == "QUARTER":
        if state.quarter_step == 0:
            mode_display = "QUARTER_MOC매도"
        elif state.quarter_step <= 10:
            mode_display = f"QUARTER_{state.quarter_step}/10"
        else:
            mode_display = "QUARTER_MOC매도"
    return {
        "mode": mode_display,
        "T": state.T,
        "star_pct": state.star_pct,
        "avg_price": state.avg_price,
        "qty": state.qty,
        "cash": state.cash,
        "cum_buy_amount": state.cum_buy_amount,
        "cum_sell_amount": getattr(state, "cum_sell_amount", 0) or 0,
        "quarter_step": state.quarter_step,
        "last_run_date": state.last_run_date,
    }


def _check_cost_consistent(avg_price: float, qty: int, cum_buy: float, cum_sell: float) -> str:
    """
    잔여 보유 검증: 매도 체결이 거의 없을 때만 평단×수량 ≈ 순투입(매수누적−매도누적) 비교 가능.

    매도누적은 매도 체결대금(수령 현금 합)이지, 매도 분의 '원가 상각액'이 아니다.
    따라서 매도가 있으면 증권사 평단×수량(잔여 물량 매입원가)과 순투입은 정의가 달라
    어긋나는 것이 정상이다 → 비교 표시 '-' (해당 없음).
    """
    cs = round(float(cum_sell or 0), 2)
    if cs > 0.01:
        return "-"
    if qty <= 0:
        return "Y" if (cum_buy - cum_sell) <= 0.01 else "N"
    expected = round(avg_price * qty, 2)
    calculated = round(cum_buy - cum_sell, 2)
    diff = abs(expected - calculated)
    tol = max(1.0, expected * 0.005)  # $1 또는 0.5% 중 큰 값
    return "Y" if diff <= tol else "N"


# ========== API 라우트 ==========
@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/api/strategy_versions")
def list_strategy_versions():
    """지원하는 무한매수법 버전 목록"""
    return {"versions": SUPPORTED_VERSIONS}


@app.get("/api/portfolios")
def list_portfolios():
    """활성 포트폴리오 목록 + 상태(T, ☆%, 모드 등)"""
    with SessionLocal() as session:
        portfolios = session.scalars(select(Portfolio).where(Portfolio.is_active == True)).all()
        result = []
        for p in portfolios:
            st = get_state_for_portfolio(session, p)
            # 싸이클 종료: qty=0 이면서 cum_buy_amount>0 (매수했던 적 있고 전량 매도됨)
            cycle_ended = st.get("qty", 0) == 0 and (st.get("cum_buy_amount") or 0) > 0
            cost_consistent = _check_cost_consistent(
                st.get("avg_price", 0) or 0,
                st.get("qty", 0) or 0,
                st.get("cum_buy_amount", 0) or 0,
                st.get("cum_sell_amount", 0) or 0,
            )
            result.append({
                "id": p.id,
                "ticker": p.ticker,
                "strategy_version": getattr(p, "strategy_version", "2.2") or "2.2",
                "seed": p.seed,
                "A": p.A,
                "R": getattr(p, "R", 10.0) or 10.0,
                "B": p.B,
                "trading_enabled": getattr(p, "trading_enabled", True),
                "cycle_ended": cycle_ended,
                "current_cycle": getattr(p, "current_cycle", 1) or 1,
                "initial_holdings_cost": getattr(p, "initial_holdings_cost", 0) or 0,
                "cost_consistent": cost_consistent,
                **st,
            })
        return result


@app.post("/api/portfolios")
def create_portfolio(data: PortfolioCreate):
    """포트폴리오 등록 (종목 추가)"""
    with SessionLocal() as session:
        version = data.strategy_version or "2.2"
        if version not in SUPPORTED_VERSIONS:
            raise HTTPException(400, f"지원하지 않는 버전: {version}. 사용가능: {SUPPORTED_VERSIONS}")
        try:
            from core.ticker_registry import assert_ticker_available, TickerConflict
            assert_ticker_available("infinite", data.ticker.upper())
        except TickerConflict as e:
            raise HTTPException(409, str(e))
        pf = Portfolio(
            ticker=data.ticker.upper(),
            strategy_version=version,
            seed=data.seed,
            A=data.A,
            R=data.R,
            fee_rate=data.fee_rate,
            ovrs_excg_cd=data.ovrs_excg_cd,
        )
        if data.already_holding:
            pf.initial_buy_done = True
            pf.trading_enabled = True
            pf.cycle_start_date = datetime.now().strftime("%Y%m%d")
        pf.initial_holdings_cost = getattr(data, "initial_holdings_cost", 0) or 0
        session.add(pf)
        session.commit()
        session.refresh(pf)
        msg = "등록 완료 (이미 보유 중 - 강제동기화를 실행하세요)" if data.already_holding else "등록 완료"
        return {"id": pf.id, "ticker": pf.ticker, "message": msg}


@app.get("/api/portfolios/{portfolio_id}")
def get_portfolio(portfolio_id: int):
    """포트폴리오 1건 상세 + 상태"""
    with SessionLocal() as session:
        pf = session.get(Portfolio, portfolio_id)
        if not pf:
            raise HTTPException(404, "포트폴리오 없음")
        st = get_state_for_portfolio(session, pf)
        return {
            "id": pf.id,
            "ticker": pf.ticker,
            "strategy_version": getattr(pf, "strategy_version", "2.2") or "2.2",
            "seed": pf.seed,
            "A": pf.A,
            "B": pf.B,
            **st,
        }


class PortfolioUpdate(BaseModel):
    """포트폴리오 수정용 (부분 업데이트)"""
    initial_holdings_cost: float | None = None


@app.patch("/api/portfolios/{portfolio_id}")
def update_portfolio(portfolio_id: int, data: PortfolioUpdate):
    """포트폴리오 설정 수정 (기존 보유 매입금액 등)"""
    with SessionLocal() as session:
        pf = session.get(Portfolio, portfolio_id)
        if not pf:
            raise HTTPException(404, "포트폴리오 없음")
        if data.initial_holdings_cost is not None:
            pf.initial_holdings_cost = max(0.0, float(data.initial_holdings_cost))
        session.commit()
        return {"message": "수정 완료"}


@app.delete("/api/portfolios/{portfolio_id}")
def delete_portfolio(portfolio_id: int):
    """포트폴리오 비활성화 (실제 삭제 아님)"""
    with SessionLocal() as session:
        pf = session.get(Portfolio, portfolio_id)
        if not pf:
            raise HTTPException(404, "포트폴리오 없음")
        pf.is_active = False
        session.commit()
        return {"message": "비활성화 완료"}


def _cancel_pending_orders_for_ticker(ticker: str, ovrs_excg_cd: str = "NASD") -> int:
    """해당 종목 미체결 주문 일괄 취소. 취소한 건수 반환."""
    try:
        cfg = get_kis_settings(DATABASE_URL)
        ctac = (cfg or {}).get("ctac_tlno") or CTAC_TLNO
        trading_mode = (cfg or {}).get("trading_mode") or TRADING_MODE
        if cfg and (cfg.get("app_key") or cfg.get("my_app")):
            client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
        else:
            client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)
        if not client.auth(ctac):
            return 0
        df = client.inquire_nccs(ctac_tlno=ctac)
        if df.empty or "pdno" not in df.columns:
            return 0
        ticker_rows = df[df["pdno"] == ticker]
        cancelled = 0
        for _, row in ticker_rows.iterrows():
            try:
                qty = row.get("nccs_qty") or row.get("ord_qty")
                if not qty:
                    continue
                res = client.order_cancel(
                    pdno=ticker,
                    orgn_odno=str(row.get("odno", row.get("ORGN_ODNO", ""))),
                    ord_qty=str(int(float(qty))),
                    ovrs_ord_unpr="0",
                    ovrs_excg_cd=ovrs_excg_cd,
                    ctac_tlno=ctac,
                )
                if res.get("rt_cd") == "0":
                    cancelled += 1
            except Exception:
                pass
        return cancelled
    except Exception:
        return 0


@app.patch("/api/portfolios/{portfolio_id}/trading")
def toggle_portfolio_trading(portfolio_id: int):
    """포트폴리오 진행 ON/OFF 토글. OFF 시 해당 종목 미체결 자동 취소"""
    with SessionLocal() as session:
        pf = session.get(Portfolio, portfolio_id)
        if not pf:
            raise HTTPException(404, "포트폴리오 없음")
        was_enabled = getattr(pf, "trading_enabled", True)
        pf.trading_enabled = not was_enabled
        session.commit()
        status = "ON" if pf.trading_enabled else "OFF"
        need_initial = pf.trading_enabled and not getattr(pf, "initial_buy_done", False)
        cancelled = 0
        if was_enabled and not pf.trading_enabled:
            cancelled = _cancel_pending_orders_for_ticker(pf.ticker, getattr(pf, "ovrs_excg_cd", "NASD") or "NASD")
        msg = f"{pf.ticker} 진행 {status}"
        if cancelled:
            msg += f" (미체결 {cancelled}건 자동 취소)"
        return {"message": msg, "trading_enabled": pf.trading_enabled,
                "need_initial_buy": need_initial, "cancelled": cancelled}


@app.post("/api/portfolios/{portfolio_id}/initial_buy")
def initial_buy(portfolio_id: int):
    """최초 시장가 매수 실행 (현재가 +5% 지정가로 즉시 체결)"""
    result = run_initial_buy(portfolio_id)
    return result


def _row_val(row, *keys, default=""):
    """KIS 응답 row에서 대소문자 무관하게 값 추출"""
    row_dict = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    for k in keys:
        k_norm = str(k).upper().replace("_", "")
        for col, val in row_dict.items():
            if str(col).upper().replace("_", "") == k_norm:
                if val is not None and str(val).strip() not in ("", "-"):
                    return val
        if k in row_dict and row_dict[k] not in (None, "", "-"):
            return row_dict[k]
    return default


def _find_col_val(row, *patterns: str):
    """컬럼명에 패턴이 포함된 첫 번째 유효값 반환"""
    row_dict = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    for col, val in row_dict.items():
        if val is None or str(val).strip() in ("", "-"):
            continue
        col_upper = str(col).upper()
        for p in patterns:
            if p.upper() in col_upper:
                return val
    return None


def _parse_kis_side(row) -> str:
    """KIS row에서 매수/매도 구분. 01/1=매도, 02/2=매수"""
    v = _row_val(row, "sll_buy_dvsn", "SLL_BUY_DVSN", "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD", default="")
    if not v:
        v = _find_col_val(row, "sll_buy", "sll", "buy_dvsn")
    s = str(v).strip() if v else ""
    try:
        n = int(float(v)) if v else -1
        if n == 1:
            return "매도"
        if n == 2:
            return "매수"
    except (TypeError, ValueError):
        pass
    if s in ("01", "1"):
        return "매도"
    if s in ("02", "2"):
        return "매수"
    return "매수"


def _parse_kis_ord_dvsn(row) -> str:
    """KIS row에서 주문구분. 34=LOC, 33=MOC, 00=지정가"""
    v = _row_val(row, "ord_dvsn", "ORD_DVSN", "ord_dvsn_cd", "ORD_DVSN_CD", "rvse_cncl_dvsn_cd", default="")
    if not v:
        v = _find_col_val(row, "ord_dvsn", "ord_dvsn")
    s = str(v).strip() if v else "00"
    try:
        n = int(float(v)) if v else 0
        if n == 34:
            return "34"
        if n == 33:
            return "33"
        if n == 0:
            return "00"
    except (TypeError, ValueError):
        pass
    if s in ("34", "33", "00", "0"):
        return s if s != "0" else "00"
    return "00"


@app.get("/api/orders/pending")
def api_orders_pending():
    """KIS 미체결 주문 실시간 조회 (등록된 포트폴리오 종목만). DB 매칭 시 구분/가격 정확 표시."""
    try:
        with SessionLocal() as session:
            registered = {p.ticker for p in session.scalars(select(Portfolio).where(Portfolio.is_active == True)).all()}
            # odno -> {side, order_type, price} (Order 테이블 기준)
            order_map = {}
            db_orders = session.execute(
                select(Order, Portfolio).join(Portfolio, Order.portfolio_id == Portfolio.id).where(
                    Order.status.in_(["pending", "submitted", "success"]),
                    Order.odno != None,
                    Order.odno != "",
                )
            ).all()
            for o, pf in db_orders:
                ono = str(o.odno or "").strip()
                if ono:
                    order_map[ono] = {
                        "side": "매수" if o.side == "buy" else "매도",
                        "order_type": o.order_type,
                        "price": float(o.price or 0),
                    }
        if not registered:
            return {"items": [], "error": ""}
        cfg = get_kis_settings(DATABASE_URL)
        ctac = (cfg or {}).get("ctac_tlno") or CTAC_TLNO
        trading_mode = (cfg or {}).get("trading_mode") or TRADING_MODE
        if cfg and (cfg.get("app_key") or cfg.get("my_app")):
            client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
        else:
            client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)
        if not client.auth(ctac):
            return {"items": [], "error": "KIS 인증 실패"}
        nccs_parts = []
        for ovrs in ["NASD", "NYSE", "AMEX"]:
            df_part = client.inquire_nccs(ovrs_excg_cd=ovrs, ctac_tlno=ctac)
            if not df_part.empty:
                nccs_parts.append(df_part)
        df = pd.concat(nccs_parts, ignore_index=True) if nccs_parts else client.inquire_nccs(ctac_tlno=ctac)
        if df.empty:
            return {"items": [], "error": ""}
        odno_col = next((c for c in df.columns if str(c).upper().replace("_", "") in ("ODNO", "ORGNODNO")), None)
        if odno_col:
            df = df.drop_duplicates(subset=[odno_col], keep="first")
        items = []
        for _, row in df.iterrows():
            pdno = str(_row_val(row, "pdno", "PDNO", default="")).strip()
            if pdno not in registered:
                continue
            odno = str(_row_val(row, "odno", "ORGN_ODNO", "ODNO", default="")).strip()
            db_info = order_map.get(odno) if odno else None
            if db_info:
                side = db_info["side"]
                order_type = db_info["order_type"]
                price = db_info["price"]
                prefix = "LOC " if order_type == "LOC" else ("MOC " if order_type == "MOC" else ("지정가 " if order_type == "LIMIT" else ""))
                side_label = prefix + side
                ord_dvsn = "34" if order_type == "LOC" else ("33" if order_type == "MOC" else "00")
                qty_raw = _row_val(row, "nccs_qty", "NCCS_QTY", "ord_qty", "ORD_QTY", default="0")
                qty = int(float(qty_raw)) if qty_raw else 0
            else:
                side = _parse_kis_side(row)
                ord_dvsn = _parse_kis_ord_dvsn(row)
                prefix = "LOC " if ord_dvsn == "34" else ("MOC " if ord_dvsn == "33" else ("지정가 " if ord_dvsn == "00" else ""))
                side_label = prefix + side
                raw_price = _row_val(row, "ord_unpr", "ovrs_ord_unpr", "ORD_UNPR", "OVRS_ORD_UNPR",
                                     "ord_prpr", "ORD_PRPR", "ft_ord_unpr", "FT_ORD_UNPR", "ft_ord_unpr3", default="")
                try:
                    price = float(raw_price) if raw_price and str(raw_price).strip() not in ("", "-") else 0.0
                except (TypeError, ValueError):
                    price = 0.0
                qty_raw = _row_val(row, "nccs_qty", "NCCS_QTY", "ord_qty", "ORD_QTY", default="0")
                qty = int(float(qty_raw)) if qty_raw else 0
            items.append({
                "ticker": pdno,
                "side": side,
                "side_label": side_label,
                "price": price,
                "ord_dvsn": ord_dvsn,
                "qty": qty,
                "order_no": odno,
                "ord_dt": str(_row_val(row, "ord_dt", "ORD_DT", default="")),
                "ord_tmd": str(_row_val(row, "ord_tmd", "ORD_TMD", default="")),
            })
        return {"items": items, "error": ""}
    except Exception as e:
        return {"items": [], "error": str(e)}


class CancelOrderItem(BaseModel):
    ticker: str
    order_no: str
    qty: int


class CancelOrdersBody(BaseModel):
    items: list[CancelOrderItem]


@app.post("/api/orders/cancel")
def api_orders_cancel(body: CancelOrdersBody):
    """미체결 주문 일괄 취소"""
    try:
        cfg = get_kis_settings(DATABASE_URL)
        ctac = (cfg or {}).get("ctac_tlno") or CTAC_TLNO
        trading_mode = (cfg or {}).get("trading_mode") or TRADING_MODE
        if cfg and (cfg.get("app_key") or cfg.get("my_app")):
            client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
        else:
            client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)
        if not client.auth(ctac):
            return {"success": False, "cancelled": [], "errors": ["KIS 인증 실패"]}
        cancelled = []
        errors = []
        for item in body.items:
            try:
                res = client.order_cancel(
                    pdno=item.ticker,
                    orgn_odno=item.order_no,
                    ord_qty=str(item.qty),
                    ovrs_ord_unpr="0",
                    ctac_tlno=ctac,
                )
                if res.get("rt_cd") == "0":
                    cancelled.append({"ticker": item.ticker, "order_no": item.order_no})
                else:
                    errors.append(f"{item.ticker}: {res.get('msg1', '취소 실패')}")
            except Exception as e:
                errors.append(f"{item.ticker}: {e}")
        return {"success": len(errors) == 0, "cancelled": cancelled, "errors": errors}
    except Exception as e:
        return {"success": False, "cancelled": [], "errors": [str(e)]}


@app.get("/api/orders/today")
def today_orders():
    """오늘 주문해야 할 사항 (포트폴리오별 주문 세트 생성)"""
    with SessionLocal() as session:
        portfolios = session.scalars(
            select(Portfolio).where(Portfolio.is_active == True, Portfolio.trading_enabled == True)
        ).all()
        today = datetime.now().strftime("%Y%m%d")
        result = []
        for pf in portfolios:
            st_dict = get_state_for_portfolio(session, pf)
            state = PortfolioState(**{k: v for k, v in st_dict.items()
                                     if k in ("avg_price", "qty", "cash", "cum_buy_amount",
                                              "T", "star_pct", "mode", "quarter_step",
                                              "quarter_base_cash", "last_run_date", "last_orders_hash")})
            state.portfolio_id = pf.id
            if st_dict.get("avg_price", 0) <= 0:
                result.append({
                    "ticker": pf.ticker,
                    "side": "-",
                    "order_type": "대기",
                    "price": 0,
                    "qty": 0,
                    "amount": 0,
                    "note": "최초매수 또는 동기화 필요",
                })
                continue
            try:
                avg = st_dict.get("avg_price", 0)
                star = st_dict.get("star_pct", 10.0)
                orders = generate_orders(pf, state, today)
                for o in orders:
                    desc = ""
                    if o.side == "buy" and avg > 0:
                        if abs(o.price - round(avg - 0.01, 2)) < 0.02:
                            desc = f"평단가 ${avg:.2f}"
                        else:
                            pct = ((o.price + 0.01) / avg - 1) * 100 if avg > 0 else 0
                            desc = f"평단가 ${avg:.2f} × {pct:+.1f}%"
                    elif o.side == "sell" and avg > 0:
                        pct = (o.price / avg - 1) * 100 if avg > 0 else 0
                        desc = f"평단가 ${avg:.2f} × {pct:+.1f}%"
                    result.append({
                        "ticker": pf.ticker,
                        "side": o.side,
                        "order_type": o.order_type,
                        "price": round(o.price, 2),
                        "qty": o.qty,
                        "amount": round(o.amount, 2) if o.amount else 0,
                        "desc": desc,
                    })
            except Exception:
                pass
        return result


@app.get("/api/trades")
def get_trades(limit: int = 50, offset: int = 0,
               date_from: str = None, date_to: str = None,
               sort: str = "desc"):
    """체결 거래내역 (등록된 포트폴리오만, 정렬/페이징/검색)"""
    from sqlalchemy import asc as sa_asc, func
    with SessionLocal() as session:
        active_pfs = session.scalars(
            select(Portfolio).where(Portfolio.is_active == True)
        ).all()
        active_ids = {p.id for p in active_pfs}
        ticker_map = {p.id: p.ticker for p in active_pfs}
        # 포트폴리오 등록일 이후 체결만 표시
        pf_created_map = {p.id: p.created_at.strftime("%Y%m%d") if p.created_at else None for p in active_pfs}
        earliest_created = min((v for v in pf_created_map.values() if v), default=None)

        base = select(Trade).where(Trade.portfolio_id.in_(active_ids))
        if earliest_created:
            base = base.where(Trade.trade_date >= earliest_created)
        if date_from:
            base = base.where(Trade.trade_date >= date_from)
        if date_to:
            base = base.where(Trade.trade_date <= date_to)

        total_count = session.scalar(select(func.count()).select_from(base.subquery()))

        order_col = Trade.trade_date
        if sort == "asc":
            stmt = base.order_by(sa_asc(order_col), sa_asc(Trade.id))
        else:
            stmt = base.order_by(desc(order_col), desc(Trade.id))

        trades = session.scalars(stmt.offset(offset).limit(limit)).all()
        return {
            "items": [
                {
                    "id": t.id,
                    "portfolio_id": t.portfolio_id,
                    "ticker": ticker_map.get(t.portfolio_id, "?"),
                    "trade_date": t.trade_date,
                    "side": t.side,
                    "order_type": t.order_type,
                    "price": t.price,
                    "order_price": getattr(t, "order_price", None),
                    "qty": t.qty,
                    "amount": round(t.price * t.qty, 2),
                    "buy_seq": getattr(t, "buy_seq", None),
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in trades
            ],
            "total_count": total_count or 0,
            "limit": limit,
            "offset": offset,
            "sort": sort,
        }


@app.delete("/api/trades/{trade_id}")
def delete_trade(trade_id: int):
    """체결 거래내역 1건 삭제 (등록된 포트폴리오 소유만)"""
    with SessionLocal() as session:
        active_ids = {p.id for p in session.scalars(select(Portfolio).where(Portfolio.is_active == True)).all()}
        trade = session.get(Trade, trade_id)
        if not trade:
            raise HTTPException(404, "거래내역 없음")
        if trade.portfolio_id not in active_ids:
            raise HTTPException(403, "해당 거래를 삭제할 수 없습니다.")
        session.delete(trade)
        session.commit()
    return {"success": True, "id": trade_id}


@app.get("/api/trades/export")
def export_trades(date_from: str = None, date_to: str = None):
    """거래내역 CSV 다운로드 (등록된 포트폴리오만)"""
    from fastapi.responses import StreamingResponse
    import io, csv
    with SessionLocal() as session:
        active_pfs = session.scalars(
            select(Portfolio).where(Portfolio.is_active == True)
        ).all()
        active_ids = {p.id for p in active_pfs}
        ticker_map = {p.id: p.ticker for p in active_pfs}
        stmt = select(Trade).where(Trade.portfolio_id.in_(active_ids)).order_by(Trade.trade_date, Trade.id)
        if date_from:
            stmt = stmt.where(Trade.trade_date >= date_from)
        if date_to:
            stmt = stmt.where(Trade.trade_date <= date_to)
        trades = session.scalars(stmt).all()

        output = io.StringIO()
        output.write('\ufeff')
        writer = csv.writer(output)
        ord_type_map = {"CCLD": "체결", "LIMIT": "지정가", "LOC": "LOC", "MOC": "MOC", "LOO": "LOO"}
        writer.writerow(["일자", "Ticker", "회차", "구분", "유형", "체결가(USD)", "주문가(USD)", "수량", "체결금액(USD)"])
        for t in trades:
            amt = round(t.price * t.qty, 2)
            order_price = getattr(t, "order_price", None)
            writer.writerow([
                t.trade_date,
                ticker_map.get(t.portfolio_id, "?"),
                getattr(t, "buy_seq", "") or "",
                "매수" if t.side == "buy" else "매도",
                ord_type_map.get(t.order_type, t.order_type),
                round(t.price, 2),
                round(order_price, 2) if order_price is not None else "",
                t.qty,
                amt,
            ])
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=trades.csv"},
        )


@app.get("/api/logs")
def get_logs(limit: int = 50, offset: int = 0,
             date_from: str = None, date_to: str = None):
    """로그 목록 (페이징 + 일자검색)"""
    with SessionLocal() as session:
        stmt = select(AppLog).order_by(desc(AppLog.created_at))
        if date_from:
            stmt = stmt.where(AppLog.created_at >= datetime.strptime(date_from, "%Y%m%d"))
        if date_to:
            dt_to = datetime.strptime(date_to, "%Y%m%d") + timedelta(days=1)
            stmt = stmt.where(AppLog.created_at < dt_to)
        logs = session.scalars(stmt.offset(offset).limit(limit)).all()
        items = []
        for l in logs:
            dt = l.created_at
            if dt:
                if dt.tzinfo is None:
                    from datetime import timezone
                    dt = datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
                                  tzinfo=timezone.utc).astimezone(KST)
                else:
                    dt = dt.astimezone(KST)
                created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                created_at = None
            items.append({"id": l.id, "portfolio_id": l.portfolio_id, "level": l.level,
                          "message": l.message, "created_at": created_at})
        return {
            "items": items,
            "limit": limit,
            "offset": offset,
        }


class SettingsUpdate(BaseModel):
    """대시보드 설정 저장용 (모두 Optional, 미입력 시 기존 유지)"""
    trading_mode: str | None = None
    my_app: str | None = None
    my_sec: str | None = None
    paper_app: str | None = None
    paper_sec: str | None = None
    my_acct_stock: str | None = None
    my_paper_stock: str | None = None
    my_prod: str | None = None
    ctac_tlno: str | None = None
    my_htsid: str | None = None


@app.get("/api/settings")
def get_settings():
    """설정 조회 (앱시크릿 마스킹 처리)"""
    return get_settings_for_display(DATABASE_URL)


@app.post("/api/settings")
def update_settings(data: SettingsUpdate):
    """설정 저장 (대시보드에서 입력한 값만 업데이트)"""
    d = {k: v for k, v in data.model_dump().items() if v is not None}
    save_settings(d, DATABASE_URL)
    reset_shared_client()
    return {"message": "저장되었습니다"}


@app.post("/api/sync")
def force_sync():
    """강제 동기화: 잔고/체결만 새로고침 (주문 제출 없음)"""
    try:
        result = run_worker_once(CTAC_TLNO, force=True)
        return result
    except Exception as e:
        logger.exception(f"강제 동기화 오류: {e}")
        return {"success": False, "portfolios": [], "errors": [str(e)]}


@app.post("/api/run_worker")
def run_worker_full():
    """전체 실행: 동기화 + 주문 제출 (스케줄된 워커와 동일)"""
    try:
        result = run_worker_once(CTAC_TLNO, force=False)
        return result
    except Exception as e:
        logger.exception(f"워커 실행 오류: {e}")
        return {"success": False, "portfolios": [], "errors": [str(e)]}


@app.post("/api/kill_switch")
def kill_switch(activate: bool = True):
    """Kill Switch: activate=True 시 긴급정지+미체결취소, False 시 해제"""
    if activate:
        kill_switch_activate()
        return {"message": "Kill Switch 활성화 - 모든 자동주문 중지 및 미체결 취소"}
    kill_switch_deactivate()
    return {"message": "Kill Switch 해제"}


@app.get("/api/kill_switch")
def kill_switch_status():
    """Kill Switch 활성화 여부"""
    return {"active": is_kill_switch_on()}


@app.get("/api/status")
def api_status(check_api: bool = True):
    """다음 워커 실행 시각, KIS API 연결 여부, 서버 시각, API 토큰 발급/만료 시각(KST)"""
    next_run = get_next_worker_run_kst()
    server_time_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S") + " KST"
    api_ok = None
    api_msg = ""
    token_issued_kst = None
    token_expired_kst = None
    try:
        cfg = get_kis_settings(DATABASE_URL)
        if cfg:
            trading_mode = cfg.get("trading_mode") or TRADING_MODE
            ctac = cfg.get("ctac_tlno") or CTAC_TLNO
            client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
        else:
            client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)
            ctac = CTAC_TLNO
        if check_api:
            api_ok = client.auth(ctac)
            api_msg = "KIS API 연결 정상" if api_ok else "KIS API 인증 실패"
        times = client.get_token_display_times()
        token_issued_kst = times.get("issued_kst")
        token_expired_kst = times.get("expired_kst")
    except Exception as e:
        if check_api:
            api_ok = False
            api_msg = f"KIS API 오류: {e}"
    return {
        "next_worker_run": next_run,
        "server_time_kst": server_time_kst,
        "token_issued_kst": token_issued_kst,
        "token_expired_kst": token_expired_kst,
        "api_ok": api_ok,
        "api_msg": api_msg,
        "kill_switch": is_kill_switch_on(),
    }


@app.get("/api/cycles")
def get_cycles(portfolio_id: int = None):
    """성공리포트: 싸이클 이력 조회"""
    with SessionLocal() as session:
        stmt = select(CycleHistory).order_by(desc(CycleHistory.end_date), CycleHistory.portfolio_id)
        if portfolio_id:
            stmt = stmt.where(CycleHistory.portfolio_id == portfolio_id)
        cycles = session.scalars(stmt).all()
        portfolios = session.scalars(select(Portfolio)).all()
        ticker_map = {p.id: p.ticker for p in portfolios}
        items = []
        for c in cycles:
            items.append({
                "id": c.id,
                "portfolio_id": c.portfolio_id,
                "ticker": ticker_map.get(c.portfolio_id, "?"),
                "cycle_number": c.cycle_number,
                "start_date": c.start_date,
                "end_date": c.end_date,
                "total_buy_amount": round(c.total_buy_amount, 2),
                "total_sell_amount": round(c.total_sell_amount, 2),
                "profit": round(c.profit, 2),
                "profit_pct": round(c.profit_pct, 2),
            })
        total_profit = sum(i["profit"] for i in items)
        total_buy = sum(i["total_buy_amount"] for i in items)
        total_sell = sum(i["total_sell_amount"] for i in items)
        return {
            "items": items,
            "summary": {
                "total_cycles": len(items),
                "total_buy": round(total_buy, 2),
                "total_sell": round(total_sell, 2),
                "total_profit": round(total_profit, 2),
                "total_profit_pct": round(total_profit / total_buy * 100, 2) if total_buy > 0 else 0,
            },
        }


@app.get("/api/cycles/{cycle_id}/trades")
def get_cycle_trades(cycle_id: int):
    """성공리포트 상세: 해당 싸이클의 거래내역"""
    with SessionLocal() as session:
        cycle = session.get(CycleHistory, cycle_id)
        if not cycle:
            raise HTTPException(404, "싸이클을 찾을 수 없습니다")
        pf = session.get(Portfolio, cycle.portfolio_id)
        ticker = pf.ticker if pf else "?"
        stmt = (select(Trade)
                .where(Trade.portfolio_id == cycle.portfolio_id,
                       Trade.trade_date >= cycle.start_date,
                       Trade.trade_date <= cycle.end_date)
                .order_by(Trade.trade_date, Trade.id))
        trades = session.scalars(stmt).all()
        return {
            "cycle": {
                "id": cycle.id,
                "ticker": ticker,
                "cycle_number": cycle.cycle_number,
                "start_date": cycle.start_date,
                "end_date": cycle.end_date,
                "total_buy_amount": round(cycle.total_buy_amount, 2),
                "total_sell_amount": round(cycle.total_sell_amount, 2),
                "profit": round(cycle.profit, 2),
                "profit_pct": round(cycle.profit_pct, 2),
            },
            "trades": [
                {
                    "trade_date": t.trade_date,
                    "side": t.side,
                    "order_type": t.order_type,
                    "price": t.price,
                    "order_price": getattr(t, "order_price", None),
                    "qty": t.qty,
                    "amount": round(t.price * t.qty, 2),
                    "buy_seq": getattr(t, "buy_seq", None),
                }
                for t in trades
            ],
        }


@app.delete("/api/cycles/{cycle_id}")
def delete_cycle_history(cycle_id: int):
    """성공리포트(싸이클 이력) 1건 수동 삭제 — 포트폴리오 초기화와 무관"""
    with SessionLocal() as session:
        c = session.get(CycleHistory, cycle_id)
        if not c:
            raise HTTPException(404, "싸이클 이력 없음")
        session.delete(c)
        session.commit()
        return {"message": "성공리포트 1건이 삭제되었습니다"}


@app.post("/api/portfolios/{portfolio_id}/reset")
def reset_portfolio(portfolio_id: int):
    """포트폴리오 초기화 (상태·주문·거래·로그). 성공리포트(CycleHistory)는 유지."""
    with SessionLocal() as session:
        pf = session.get(Portfolio, portfolio_id)
        if not pf:
            raise HTTPException(404, "포트폴리오 없음")

        session.execute(
            Order.__table__.delete().where(Order.portfolio_id == portfolio_id)
        )
        session.execute(
            Trade.__table__.delete().where(Trade.portfolio_id == portfolio_id)
        )
        session.execute(
            PortfolioState.__table__.delete().where(PortfolioState.portfolio_id == portfolio_id)
        )
        session.execute(
            AppLog.__table__.delete().where(AppLog.portfolio_id == portfolio_id)
        )

        pf.initial_buy_done = False
        # 성공리포트(CycleHistory) 영구보존 — 같은 portfolio_id의 옛 싸이클 번호와
        # 충돌하지 않도록 max+1에서 시작. (기존 hardcoded =1은 새 싸이클 종료시 cycle_history 미삽입 버그)
        _max_cy = session.scalar(
            select(func.max(CycleHistory.cycle_number)).where(CycleHistory.portfolio_id == portfolio_id)
        )
        pf.current_cycle = int(_max_cy or 0) + 1
        pf.cycle_start_date = None
        pf.cycle_start_trade_id = None
        pf.trading_enabled = False
        session.commit()
        return {"message": f"{pf.ticker} 포트폴리오가 초기화되었습니다 (성공리포트는 유지됩니다)"}


@app.get("/api/account_summary")
def account_summary():
    """
    계좌 요약 (KIS API 해외주식 잔고 기준)
    - 주식평가: ovrs_stck_evlu_amt 합계
    - 매입금액: frcr_pchs_amt1 합계
    - 평가손익: frcr_evlu_pfls_amt 합계 (or tot_evlu_pfls_amt)
    - 손익률: tot_pftrt (or 평가손익/매입금액)
    """
    summary = get_account_summary(DATABASE_URL)
    stock_evlu = 0.0
    buy_amt = 0.0
    cash = 0.0
    tot_asst = 0.0
    pnl = 0.0
    pnl_rt = 0.0
    updated_at = None
    if summary:
        stock_evlu = float(summary.get("stock_evlu", 0) or 0)
        buy_amt = float(summary.get("buy_amt", 0) or 0)
        cash = float(summary.get("cash", 0) or 0)
        tot_asst = float(summary.get("tot_asst_amt", 0) or 0)
        pnl = float(summary.get("pnl", 0) or 0)
        pnl_rt = float(summary.get("pnl_rt", 0) or 0)
        updated_at = summary.get("updated_at")
        if stock_evlu <= 0:
            stock_evlu = float(summary.get("tot_evlu_amt", 0) or 0)
    exrt = float(summary.get("exrt", 0) or 0) if summary else 0.0
    tot_evlu = tot_asst if tot_asst > 0 else (stock_evlu + cash)
    return {
        "tot_evlu": round(tot_evlu, 2),
        "stock_evlu": round(stock_evlu, 2),
        "cash": round(cash, 2),
        "buy_amt": round(buy_amt, 2),
        "pnl": round(pnl, 2),
        "pnl_rt": round(pnl_rt, 2),
        "exrt": round(exrt, 2),
        "updated_at": updated_at,
    }


@app.get("/api/debug/present_balance")
def debug_present_balance():
    """체결기준잔고 API 원본 응답 확인용 (디버그)"""
    try:
        cfg = get_kis_settings(DATABASE_URL)
        trading_mode = TRADING_MODE
        ctac_tlno = CTAC_TLNO
        if cfg and (cfg.get("app_key") or cfg.get("my_app")):
            trading_mode = cfg.get("trading_mode") or TRADING_MODE
            ctac_tlno = cfg.get("ctac_tlno") or CTAC_TLNO
            client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
        else:
            client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv=TRADING_MODE)
        if not client.auth(ctac_tlno):
            return {"error": "KIS 인증 실패"}
        cano, acnt_prdt_cd = client._get_account()
        tr_id = "VTRP6504R" if client.env_dv == "demo" else "CTRP6504R"
        res = client._request(
            "GET",
            "/uapi/overseas-stock/v1/trading/inquire-present-balance",
            tr_id,
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "WCRC_FRCR_DVSN_CD": "02",
                "NATN_CD": "840",
                "TR_MKET_CD": "00",
                "INQR_DVSN_CD": "00",
            },
            ctac_tlno=ctac_tlno,
        )
        return {
            "rt_cd": res.get("rt_cd"),
            "msg1": res.get("msg1"),
            "output3": res.get("output3"),
            "output2": res.get("output2"),
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}


# ========== 대시보드 (HTML) ==========
_DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


def _load_dashboard_html() -> str:
    content = _DASHBOARD_PATH.read_text(encoding="utf-8")
    return content.replace("__TRADING_MODE__", TRADING_MODE)


_OLD_INLINE_HTML = """
<head>
  <meta charset="utf-8">
  <title>무한매수법 V2.2</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: 'Malgun Gothic', '맑은 고딕', 'Apple SD Gothic Neo', sans-serif; margin: 20px; background: #0f1419; color: #e6edf3; font-size: 14px; }
    h1 { color: #58a6ff; font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; font-size: 22px; }
    h2 { color: #58a6ff; font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; font-size: 17px; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; overflow: auto; }
    #portfolios { overflow: auto; margin-bottom: 16px; }
    table { width: 100%; border-collapse: collapse; font-family: 'Dotum', '돋움', 'Malgun Gothic', monospace; font-size: 14px; }
    th { padding: 7px 8px; text-align: left; border-bottom: 2px solid #30363d; color: #8b949e; font-size: 12px; text-transform: uppercase; font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; }
    td { padding: 7px 8px; text-align: left; border-bottom: 1px solid #21262d; letter-spacing: -0.3px; }
    .btn { padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer; font-weight: 600; margin-right: 8px; font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; font-size: 13px; }
    .btn-sm { padding: 4px 10px; font-size: 12px; }
    .btn-danger { background: #da3633; color: white; }
    .btn-primary { background: #238636; color: white; }
    .btn-secondary { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
    .kill-on { color: #f85149; font-weight: bold; }
    .log { font-family: 'Dotum', '돋움', 'D2Coding', monospace; font-size: 13px; background: #0d1117; padding: 12px; overflow-x: auto; max-height: 300px; overflow-y: auto; border-radius: 6px; letter-spacing: -0.3px; }
    .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 1000; justify-content: center; align-items: center; }
    .modal.show { display: flex; }
    .modal-content { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 24px; max-width: 500px; width: 90%; max-height: 90vh; overflow-y: auto; font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; }
    .modal h2 { margin-top: 0; }
    .form-row { margin-bottom: 12px; }
    .form-row label { display: block; margin-bottom: 4px; color: #8b949e; font-size: 13px; }
    .form-row input, .form-row select { width: 100%; padding: 8px 12px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #e6edf3; font-family: 'Dotum', '돋움', monospace; }
    .form-hint { font-size: 11px; color: #6e7681; margin-top: 4px; }
    .section-title { color: #8b949e; font-size: 12px; margin: 16px 0 8px; border-bottom: 1px solid #30363d; padding-bottom: 4px; }
    .summary { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 12px; padding-top: 12px; border-top: 1px solid #30363d; }
    .summary span { font-size: 15px; font-family: 'Dotum', '돋움', monospace; }
    .pnl-plus { color: #3fb950; }
    .pnl-minus { color: #f85149; }
    .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; }
    .badge-cycle { background: #238636; color: white; }
    .toggle-on { background: #238636; color: white; padding: 2px 8px; border-radius: 4px; font-size: 12px; cursor: pointer; }
    .toggle-off { background: #484f58; color: #8b949e; padding: 2px 8px; border-radius: 4px; font-size: 12px; cursor: pointer; }
    .buy { color: #f85149; }
    .sell { color: #3fb950; }
    .date-filter { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; position: relative; z-index: 10; font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; }
    .date-filter input { padding: 6px 10px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #e6edf3; font-size: 13px; font-family: 'Dotum', '돋움', monospace; }
    .date-filter .btn { position: relative; z-index: 11; pointer-events: auto; }
    .paging { display: flex; gap: 4px; margin-top: 8px; align-items: center; flex-wrap: wrap; }
  </style>
</head>
<body>
  <h1>무한매수법 V2.2 대시보드</h1>
  <div class="card">
    <p>
       <button class="btn btn-secondary" id="btn-open-settings">설정 (계좌/앱키)</button>
       모드: <strong id="trading-mode">__TRADING_MODE__</strong> | 
       Kill Switch: <span id="ks-status">확인중...</span>
       <button class="btn btn-danger" id="btn-kill-on">긴급정지</button>
       <button class="btn btn-secondary" id="btn-kill-off">해제</button>
       <button class="btn btn-primary" id="btn-force-sync">강제 동기화</button>
    </p>
    <div id="account-summary" class="summary">
      <span>계좌총액: <strong id="acct-tot">-</strong></span>
      <span>주식평가: <strong id="acct-stock">-</strong></span>
      <span>예수금: <strong id="acct-cash">-</strong></span>
      <span>매입금액: <strong id="acct-buy">-</strong></span>
      <span>평가손익: <strong id="acct-pnl">-</strong> (<span id="acct-pnl-pct">-</span>%)</span>
      <span style="color:#8b949e;font-size:12px;" id="acct-exrt"></span>
      <span class="form-hint" id="acct-updated">동기화 후 표시</span>
    </div>
  </div>

  <div id="settings-modal" class="modal">
    <div class="modal-content">
      <h2>한국투자증권 API 설정</h2>
      <p class="form-hint">KIS Developers(apiportal.koreainvestment.com)에서 앱키/시크릿을 발급받아 입력하세요.</p>
      <form id="settings-form">
        <div class="section-title">기본</div>
        <div class="form-row"><label>운영 모드</label><select name="trading_mode" id="set-trading_mode"><option value="demo">모의투자 (demo)</option><option value="real">실전투자 (real)</option></select></div>
        <div class="form-row"><label>연락처 (주문 시 필수)</label><input type="text" name="ctac_tlno" id="set-ctac_tlno" placeholder="01012345678" /></div>
        <div class="section-title">실전투자</div>
        <div class="form-row"><label>실전 앱키</label><input type="text" name="my_app" id="set-my_app" placeholder="PS..." autocomplete="off" /></div>
        <div class="form-row"><label>실전 앱시크릿</label><input type="password" name="my_sec" id="set-my_sec" autocomplete="new-password" /><div class="form-hint">변경 시에만 입력</div></div>
        <div class="form-row"><label>실전 계좌번호 (앞 8자리)</label><input type="text" name="my_acct_stock" id="set-my_acct_stock" maxlength="8" /></div>
        <div class="section-title">모의투자</div>
        <div class="form-row"><label>모의 앱키</label><input type="text" name="paper_app" id="set-paper_app" autocomplete="off" /></div>
        <div class="form-row"><label>모의 앱시크릿</label><input type="password" name="paper_sec" id="set-paper_sec" autocomplete="new-password" /><div class="form-hint">변경 시에만 입력</div></div>
        <div class="form-row"><label>모의 계좌번호 (앞 8자리)</label><input type="text" name="my_paper_stock" id="set-my_paper_stock" maxlength="8" /></div>
        <div class="form-row"><label>계좌상품코드</label><input type="text" name="my_prod" id="set-my_prod" value="01" maxlength="2" /><div class="form-hint">01: 종합계좌 (기본)</div></div>
        <div style="margin-top:20px"><button type="submit" class="btn btn-primary">저장</button> <button type="button" class="btn btn-secondary" id="btn-close-settings">취소</button></div>
      </form>
    </div>
  </div>

  <h2>포트폴리오 <button class="btn btn-secondary btn-sm" id="btn-add-portfolio">+ 추가</button></h2>
  <div id="portfolios"></div>

  <div id="add-portfolio-modal" class="modal">
    <div class="modal-content">
      <h2>포트폴리오 추가</h2>
      <form id="add-portfolio-form">
        <div class="form-row"><label>종목 (Ticker)</label><input type="text" name="ticker" placeholder="SOXL, AAPL" required /></div>
        <div class="form-row"><label>무한매수법 버전</label><select name="strategy_version" id="add-strategy_version"></select></div>
        <div class="form-row"><label>총 투자금 (Seed)</label><input type="number" name="seed" placeholder="4000" required /></div>
        <div class="form-row"><label>분할 회차 (A)</label><input type="number" name="A" value="40" /></div>
        <div class="form-row"><label>목표수익률 (%)</label><input type="number" name="R" value="10" min="1" max="100" /><div class="form-hint">정수 입력. 10 = 10%, 12 = 12%. ☆%와 LIMIT 매도가의 기준이 됩니다.</div></div>
        <div class="form-row"><label><input type="checkbox" name="already_holding" id="add-already-holding" /> 이미 보유 중인 종목 (최초매수 건너뜀)</label><div class="form-hint">계좌에 이미 보유하고 있는 종목을 등록할 때 체크. 등록 후 강제동기화를 실행하면 평단가/수량이 자동으로 반영됩니다.</div></div>
        <div class="form-row" id="add-initial-cost-row" style="display:none"><label>기존 보유분 매입금액 ($)</label><input type="number" name="initial_holdings_cost" id="add-initial-holdings-cost" placeholder="2000" step="0.01" min="0" /><div class="form-hint">현재 보유 중인 물량의 총 매입금액. T 계산 시 cum_buy에 포함됩니다.</div></div>
        <div style="margin-top:16px"><button type="submit" class="btn btn-primary">등록</button> <button type="button" class="btn btn-secondary" id="btn-close-add-portfolio">취소</button></div>
      </form>
    </div>
  </div>

  <h2>오늘 주문 (예정)</h2>
  <div id="orders" class="card"></div>

  <h2>거래내역 (체결)</h2>
  <div class="date-filter" id="trade-filter">
    <input type="date" id="trade-from" /> ~ <input type="date" id="trade-to" />
    <a href="javascript:void(0)" class="btn btn-primary btn-sm" style="text-decoration:none;display:inline-block;" onclick="loadTrades(true)">검색</a>
    <a href="javascript:void(0)" class="btn btn-secondary btn-sm" style="text-decoration:none;display:inline-block;" onclick="resetTradeSearch()">초기화</a>
    <a href="javascript:void(0)" class="btn btn-secondary btn-sm" style="text-decoration:none;display:inline-block;" onclick="exportTrades()">Excel 다운로드</a>
    <span style="margin-left:auto;">
      <select id="trade-page-size" onchange="changeTradePageSize()" style="padding:4px 8px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#e6edf3;font-size:12px;">
        <option value="10">10개</option>
        <option value="20">20개</option>
        <option value="50" selected>50개</option>
      </select>
    </span>
  </div>
  <div id="trades" class="card"></div>
  <div class="paging" id="trade-paging"></div>

  <h2>성공리포트</h2>
  <div id="cycles" class="card"></div>

  <div id="edit-initial-cost-modal" class="modal">
    <div class="modal-content">
      <h2>기존 보유분 매입금액</h2>
      <p class="form-hint" style="margin-bottom:14px">포트폴리오 추가 시점에 이미 보유하고 있던 물량의 총 매입금액. T 계산 시 cum_buy에 포함됩니다. 저장 후 <strong>강제 동기화</strong>를 실행하면 T/☆%가 갱신됩니다.</p>
      <form id="edit-initial-cost-form">
        <input type="hidden" id="edit-initial-cost-pf-id" />
        <div class="form-row"><label>종목</label><span id="edit-initial-cost-ticker" class="text-mono"></span></div>
        <div class="form-row"><label>기존 보유분 매입금액 ($)</label><input type="number" id="edit-initial-cost-value" placeholder="1943.16" step="0.01" min="0" required /></div>
        <div style="margin-top:16px"><button type="submit" class="btn btn-primary">저장</button> <button type="button" class="btn btn-secondary" id="btn-close-edit-initial-cost">취소</button></div>
      </form>
    </div>
  </div>

  <div id="cycle-detail-modal" class="modal">
    <div class="modal-content" style="max-width:700px;">
      <h2 id="cycle-detail-title">싸이클 상세</h2>
      <div id="cycle-detail-summary" style="margin-bottom:12px;"></div>
      <div id="cycle-detail-trades"></div>
      <div style="margin-top:16px;text-align:right;"><button class="btn btn-secondary" onclick="document.getElementById('cycle-detail-modal').classList.remove('show')">닫기</button></div>
    </div>
  </div>

  <h2>로그</h2>
  <div class="date-filter" id="log-filter">
    <input type="date" id="log-from" /> ~ <input type="date" id="log-to" />
    <a href="javascript:void(0)" class="btn btn-primary btn-sm" style="text-decoration:none;display:inline-block;" onclick="loadLogs(true)">검색</a>
    <a href="javascript:void(0)" class="btn btn-secondary btn-sm" style="text-decoration:none;display:inline-block;" onclick="logSearchMode=false;document.getElementById('log-from').value='';document.getElementById('log-to').value='';loadLogs();">초기화</a>
  </div>
  <div id="logs" class="log"></div>
  <div class="paging" id="log-paging"></div>

  <script>
    const PH = '********';
    var logOffset = 0;
    async function fetchJSON(p) { var r = await fetch(p); if (!r.ok) throw new Error(p+' '+r.status); return r.json(); }
    function fmtOrdType(t) { var m = {'CCLD':'체결','LIMIT':'지정가','LOC':'LOC','MOC':'MOC','LOO':'LOO'}; return m[t] || t; }

    async function openSettings() {
      try {
        var d = await fetchJSON('/api/settings');
        ['trading_mode','ctac_tlno','my_app','my_sec','paper_app','paper_sec','my_acct_stock','my_paper_stock','my_prod'].forEach(function(k){
          var el = document.getElementById('set-'+k);
          if (!el) return;
          var v = d[k]||'';
          el.value = (k==='my_sec'||k==='paper_sec') && v && v.includes('*') ? '' : v;
          if (k==='my_sec'||k==='paper_sec') el.placeholder = v ? PH+' (변경 시 입력)' : '앱시크릿 입력';
        });
        document.getElementById('settings-modal').classList.add('show');
      } catch(e) { alert('설정 불러오기 실패: '+e.message); }
    }
    function closeSettings() { document.getElementById('settings-modal').classList.remove('show'); }
    async function saveSettings(e) {
      e.preventDefault();
      var fd = new FormData(e.target), body = {};
      fd.forEach(function(v,k){ if ((k==='my_sec'||k==='paper_sec') ? (v&&v!==PH) : v) body[k]=v; });
      var r = await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      var d = await r.json(); alert(d.message||'저장됨'); closeSettings();
      document.getElementById('trading-mode').textContent = body.trading_mode || document.getElementById('set-trading_mode').value;
    }
    async function killSwitchStatus() {
      try { var d = await fetchJSON('/api/kill_switch'); document.getElementById('ks-status').innerHTML = d.active ? '<span class="kill-on">ON</span>' : 'OFF'; }
      catch(e) { document.getElementById('ks-status').innerHTML = '-'; }
    }
    async function killSwitch(on) { await fetch('/api/kill_switch?activate='+on,{method:'POST'}); killSwitchStatus(); }
    async function forceSync() {
      document.getElementById('btn-force-sync').textContent = '동기화 중...';
      try {
        var r = await fetch('/api/sync',{method:'POST'});
        if (!r.ok) { var txt = await r.text(); alert('동기화 실패 (서버 오류):\\n'+txt); document.getElementById('btn-force-sync').textContent = '강제 동기화'; return; }
        var d = await r.json();
        var msg = d.success ? '동기화 완료' : '동기화 실패';
        if (d.portfolios && d.portfolios.length) msg += '\\n포트폴리오: ' + d.portfolios.map(function(p){return p.ticker+(p.synced?' (동기화됨)':'');}).join(', ');
        if (d.errors && d.errors.length) msg += '\\n오류: ' + d.errors.join('\\n');
        alert(msg); loadAll();
      } catch(e) { alert('동기화 오류: '+e.message); }
      document.getElementById('btn-force-sync').textContent = '강제 동기화';
    }
    async function openAddPortfolio() {
      try { var ver = await fetchJSON('/api/strategy_versions');
        var sel = document.getElementById('add-strategy_version');
        sel.innerHTML = ver.versions.map(function(v){return '<option value="'+v+'">'+v+'</option>';}).join('');
        document.getElementById('add-portfolio-modal').classList.add('show');
        document.getElementById('add-initial-cost-row').style.display = document.getElementById('add-already-holding').checked ? '' : 'none';
      } catch(e) { alert('버전 불러오기 실패: '+e.message); }
    }
    document.getElementById('add-already-holding').addEventListener('change', function() {
      document.getElementById('add-initial-cost-row').style.display = this.checked ? '' : 'none';
    });
    function closeAddPortfolio() { document.getElementById('add-portfolio-modal').classList.remove('show'); }
    async function addPortfolio(e) {
      e.preventDefault();
      var fd = new FormData(e.target), ticker = (fd.get('ticker')||'').toString().toUpperCase();
      if (!ticker) { alert('종목을 입력하세요'); return; }
      var alreadyHolding = document.getElementById('add-already-holding').checked;
      var initCost = parseFloat(fd.get('initial_holdings_cost')||0)||0;
      var body = {ticker:ticker, strategy_version:fd.get('strategy_version')||'2.2', seed:parseFloat(fd.get('seed'))||4000, A:parseInt(fd.get('A')||40), R:parseInt(fd.get('R')||10), already_holding:alreadyHolding, initial_holdings_cost:initCost};
      var r = await fetch('/api/portfolios',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      var d = await r.json();
      if (r.ok) { alert('등록 완료'); closeAddPortfolio(); loadAll(); } else { alert(d.detail||JSON.stringify(d)); }
    }
    async function loadAccountSummary() {
      try { var d = await fetchJSON('/api/account_summary');
        var fmt = function(v) { return v > 0 ? '$'+v.toLocaleString(undefined,{minimumFractionDigits:2}) : '-'; };
        document.getElementById('acct-tot').textContent = fmt(d.tot_evlu);
        document.getElementById('acct-stock').textContent = fmt(d.stock_evlu);
        document.getElementById('acct-cash').textContent = fmt(d.cash);
        document.getElementById('acct-buy').textContent = fmt(d.buy_amt);
        var pnl = d.pnl, el = document.getElementById('acct-pnl');
        if (d.updated_at && pnl != null) {
          el.textContent = (pnl>=0?'+':'-')+'$'+Math.abs(pnl).toLocaleString(undefined,{minimumFractionDigits:2});
          el.className = pnl>0?'pnl-plus':pnl<0?'pnl-minus':'';
        } else { el.textContent = '-'; el.className = ''; }
        var rt = d.pnl_rt;
        document.getElementById('acct-pnl-pct').textContent = (d.updated_at && rt != null) ? (rt>=0?'+':'')+rt.toFixed(2) : '-';
        if (d.exrt && d.exrt > 0 && d.updated_at) {
          var exrtDate = d.updated_at.replace('T',' ').slice(0,10);
          document.getElementById('acct-exrt').textContent = '환율: ' + d.exrt.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) + ' (' + exrtDate + ' 기준)';
        }
        if (d.updated_at) {
          document.getElementById('acct-updated').textContent = d.updated_at.replace('T',' ').slice(0,19);
        } else {
          document.getElementById('acct-updated').innerHTML = '<span style="color:#f0883e">설정에서 KIS 앱키 입력 후 강제동기화를 실행하세요</span>';
        }
      } catch(_){}
    }
    function openEditInitialCost(id, ticker, currentVal) {
      document.getElementById('edit-initial-cost-pf-id').value = id;
      document.getElementById('edit-initial-cost-ticker').textContent = ticker;
      document.getElementById('edit-initial-cost-value').value = currentVal || '';
      document.getElementById('edit-initial-cost-modal').classList.add('show');
    }
    function closeEditInitialCost() { document.getElementById('edit-initial-cost-modal').classList.remove('show'); }
    async function saveInitialCost(e) {
      e.preventDefault();
      var id = parseInt(document.getElementById('edit-initial-cost-pf-id').value, 10);
      var val = parseFloat(document.getElementById('edit-initial-cost-value').value) || 0;
      var r = await fetch('/api/portfolios/'+id, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({initial_holdings_cost:val}) });
      var d = await r.json();
      if (r.ok) { alert('저장됨. 강제 동기화를 실행하면 T/☆%가 갱신됩니다.'); closeEditInitialCost(); loadAll(); }
      else { alert(d.detail||JSON.stringify(d)); }
    }
    async function deletePortfolio(id, ticker) {
      if (!confirm(ticker+' 포트폴리오를 삭제할까요?')) return;
      var r = await fetch('/api/portfolios/'+id,{method:'DELETE'}); var d = await r.json(); alert(d.message||'삭제됨'); loadAll();
    }
    async function resetPortfolio(id, ticker) {
      if (!confirm(ticker+' 포트폴리오를 초기화할까요?\\n(상태·주문·거래·로그 삭제. 성공리포트는 유지됩니다)')) return;
      if (!confirm('정말로 초기화하시겠습니까? 이 작업은 되돌릴 수 없습니다.')) return;
      var r = await fetch('/api/portfolios/'+id+'/reset',{method:'POST'}); var d = await r.json(); alert(d.message); loadAll();
    }
    async function toggleTrading(id, ticker) {
      var r = await fetch('/api/portfolios/'+id+'/trading',{method:'PATCH'}); var d = await r.json();
      if (r.ok) {
        if (d.need_initial_buy && confirm(ticker+' 최초 시장가 매수를 실행할까요?\\n현재가 +5% 가격으로 즉시 체결되도록 주문합니다.\\n(실제 체결가는 동기화 시 반영)')) {
          var r2 = await fetch('/api/portfolios/'+id+'/initial_buy',{method:'POST'}); var d2 = await r2.json(); alert(d2.message);
        } else if (d.cancelled && d.cancelled > 0) {
          alert(d.message);
        }
        loadAll();
      } else { alert(d.detail||JSON.stringify(d)); }
    }
    async function loadPortfolios() {
      var data = await fetchJSON('/api/portfolios');
      if (!data.length) { document.getElementById('portfolios').innerHTML = '<p>등록된 포트폴리오 없음. + 추가 버튼으로 등록하세요.</p>'; return; }
      var h = '<table><tr><th>진행</th><th>Ticker</th><th>싸이클</th><th>Seed</th><th>1회(B)</th><th>목표%</th><th>평단가</th><th>수량</th><th>매수누적</th><th>매도누적</th><th>순투입</th><th>T</th><th>☆%</th><th>모드</th><th>상태</th><th></th></tr>';
      data.forEach(function(p) {
        var te = p.trading_enabled !== false;
        var tog = '<span class="'+(te?'toggle-on':'toggle-off')+'" onclick="toggleTrading('+p.id+',\\''+p.ticker+'\\')">'+(te?'ON':'OFF')+'</span>';
        var cyc = p.cycle_ended ? ' <span class="badge badge-cycle">종료</span>' : '';
        var B = p.B > 0 ? '$'+p.B.toFixed(0) : '-';
        var cb = +(p.cum_buy_amount||0), cs = +(p.cum_sell_amount||0);
        var cumBuy = cb > 0 ? '$'+cb.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) : ((cb === 0 && cs === 0) ? '$0.00' : '-');
        var cumSell = cs > 0 ? '$'+cs.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) : ((cb === 0 && cs === 0) ? '$0.00' : '-');
        var net = Math.round((cb - cs) * 100) / 100;
        var cumNet = (cb > 0 || cs > 0) ? '$'+net.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) : ((cb === 0 && cs === 0) ? '$0.00' : '-');
        h += '<tr><td>'+tog+'</td><td><strong>'+p.ticker+'</strong></td><td>#'+(p.current_cycle||1)+'</td><td>$'+p.seed.toLocaleString()+'</td><td>'+B+'</td>';
        h += '<td>'+(p.R||10)+'%</td><td>'+(p.avg_price>0?'$'+p.avg_price.toFixed(2):'-')+'</td><td>'+p.qty+'</td><td>'+cumBuy+'</td><td>'+cumSell+'</td><td>'+cumNet+'</td>';
        h += '<td>'+p.T+'</td><td>'+p.star_pct.toFixed(1)+'%</td><td>'+p.mode+'</td><td>'+cyc+'</td>';
        h += '<td><button class="btn btn-secondary btn-sm" onclick="openEditInitialCost('+p.id+',\\''+p.ticker+'\\','+(p.initial_holdings_cost||0)+')">기존보유</button> <button class="btn btn-secondary btn-sm" onclick="deletePortfolio('+p.id+',\\''+p.ticker+'\\')">삭제</button> <button class="btn btn-danger btn-sm" onclick="resetPortfolio('+p.id+',\\''+p.ticker+'\\')">초기화</button></td></tr>';
      });
      h += '</table>';
      document.getElementById('portfolios').innerHTML = h;
    }
    async function loadOrders() {
      var data = await fetchJSON('/api/orders/today');
      if (!data.length) { document.getElementById('orders').innerHTML = '<p>오늘 주문 예정 없음</p>'; return; }
      var h = '<table><tr><th>Ticker</th><th>구분</th><th>유형</th><th>가격</th><th>산출근거</th><th>수량</th><th>금액</th></tr>';
      data.forEach(function(o) {
        var cls = o.side==='buy'?'buy':(o.side==='sell'?'sell':'');
        var label = o.side==='buy'?'매수':(o.side==='sell'?'매도':'-');
        var note = o.note ? ' <span class="form-hint">('+o.note+')</span>' : '';
        var desc = o.desc ? '<span class="form-hint">'+o.desc+'</span>' : '';
        h += '<tr><td>'+o.ticker+note+'</td><td class="'+cls+'">'+label+'</td><td>'+o.order_type+'</td>';
        h += '<td>'+(o.price>0?'$'+(o.price).toFixed(2):'-')+'</td><td>'+desc+'</td><td>'+(o.qty>0?o.qty+'주':'-')+'</td><td>'+(o.amount>0?'$'+o.amount.toLocaleString():'-')+'</td></tr>';
      });
      h += '</table>';
      document.getElementById('orders').innerHTML = h;
    }
    var tradeSearchMode = false;
    var tradeSort = 'desc';
    var tradePageSize = 50;
    var tradePage = 1;
    function changeTradePageSize() { tradePageSize = parseInt(document.getElementById('trade-page-size').value); tradePage = 1; loadTrades(); }
    function toggleTradeSort() { tradeSort = (tradeSort==='desc'?'asc':'desc'); tradePage = 1; loadTrades(); }
    function gotoTradePage(p) { tradePage = p; loadTrades(); }
    async function loadTrades(reset) {
      if (reset) { tradePage = 1; tradeSearchMode = true; }
      var offset = (tradePage - 1) * tradePageSize;
      var url = '/api/trades?limit='+tradePageSize+'&offset='+offset+'&sort='+tradeSort;
      if (tradeSearchMode) {
        var tfrom = document.getElementById('trade-from').value;
        var tto = document.getElementById('trade-to').value;
        if (tfrom) url += '&date_from='+tfrom.replace(/-/g,'');
        if (tto) url += '&date_to='+tto.replace(/-/g,'');
      }
      try {
        var r = await fetch(url);
        var d = await r.json();
        if (!d.items || !d.items.length) {
          document.getElementById('trades').innerHTML = '<p>거래내역 없음'+(tradeSearchMode?' (검색 결과 없음)':'')+'</p>';
          document.getElementById('trade-paging').innerHTML='';
          return;
        }
        var sortIcon = tradeSort==='asc' ? '\\u25B2' : '\\u25BC';
        var h = '<table><tr><th style="cursor:pointer;" onclick="toggleTradeSort()">일자 '+sortIcon+'</th><th>Ticker</th><th>회차</th><th>구분</th><th>유형</th><th>체결가</th><th>주문가</th><th>수량</th><th>체결금액</th><th style="width:60px;">삭제</th></tr>';
        d.items.forEach(function(t) {
          var cls = t.side==='buy'?'buy':'sell';
          var seqCell = t.buy_seq || '-';
          var priceCell = (t.price!=null && t.price!=='') ? '$'+Number(t.price).toFixed(2) : '-';
          var orderPriceCell = (t.order_price!=null && t.order_price!=='') ? '$'+Number(t.order_price).toFixed(2) : '-';
          var amtVal = t.amount != null ? t.amount : (t.price||0)*t.qty;
          h += '<tr><td>'+t.trade_date+'</td><td><strong>'+(t.ticker||'?')+'</strong></td><td>'+seqCell+'</td><td class="'+cls+'">'+(t.side==='buy'?'매수':'매도')+'</td><td>'+fmtOrdType(t.order_type)+'</td>';
          h += '<td>'+priceCell+'</td><td>'+orderPriceCell+'</td><td>'+t.qty+'주</td><td>$'+Number(amtVal).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+'</td>';
          h += '<td><button type="button" class="btn btn-secondary btn-sm" style="padding:2px 8px;font-size:12px;" onclick="deleteTradeRow('+t.id+')" title="이 행만 삭제">삭제</button></td></tr>';
        });
        h += '</table>';
        document.getElementById('trades').innerHTML = h;
        var totalCount = d.total_count || 0;
        var totalPages = Math.ceil(totalCount / tradePageSize);
        var pg = '<span style="color:#8b949e;font-size:12px;">총 '+totalCount+'건</span> ';
        if (totalPages > 1) {
          if (tradePage > 1) pg += '<a href="javascript:void(0)" class="btn btn-secondary btn-sm" style="text-decoration:none;display:inline-block;min-width:30px;text-align:center;" onclick="gotoTradePage('+(tradePage-1)+')">◀</a> ';
          var startP = Math.max(1, tradePage - 4);
          var endP = Math.min(totalPages, startP + 9);
          if (endP - startP < 9) startP = Math.max(1, endP - 9);
          for (var i = startP; i <= endP; i++) {
            if (i === tradePage) pg += '<span class="btn btn-primary btn-sm" style="min-width:30px;text-align:center;">'+i+'</span> ';
            else pg += '<a href="javascript:void(0)" class="btn btn-secondary btn-sm" style="text-decoration:none;display:inline-block;min-width:30px;text-align:center;" onclick="gotoTradePage('+i+')">'+i+'</a> ';
          }
          if (tradePage < totalPages) pg += '<a href="javascript:void(0)" class="btn btn-secondary btn-sm" style="text-decoration:none;display:inline-block;min-width:30px;text-align:center;" onclick="gotoTradePage('+(tradePage+1)+')">▶</a>';
        }
        document.getElementById('trade-paging').innerHTML = pg;
      } catch(e) { document.getElementById('trades').innerHTML = '<p>거래내역 로드 실패: '+e.message+'</p>'; }
    }
    async function deleteTradeRow(id) {
      if (!confirm('이 체결 건을 삭제할까요? (DB에서만 삭제되며 복구되지 않습니다)')) return;
      try {
        var r = await fetch('/api/trades/'+id, { method: 'DELETE' });
        var d = await r.json().catch(function(){ return {}; });
        if (r.ok && d.success) { loadTrades(); return; }
        alert(d.detail || (typeof d.detail==='object' && d.detail.message) || '삭제 실패');
      } catch(e) { alert('삭제 요청 실패: '+e.message); }
    }
    function resetTradeSearch() { tradeSearchMode = false; tradePage = 1; document.getElementById('trade-from').value=''; document.getElementById('trade-to').value=''; loadTrades(); }
    function exportTrades() {
      var url = '/api/trades/export';
      var params = [];
      if (tradeSearchMode) {
        var tfrom = document.getElementById('trade-from').value;
        var tto = document.getElementById('trade-to').value;
        if (tfrom) params.push('date_from='+tfrom.replace(/-/g,''));
        if (tto) params.push('date_to='+tto.replace(/-/g,''));
      }
      if (params.length) url += '?' + params.join('&');
      window.open(url, '_blank');
    }
    var logSearchMode = false;
    async function loadLogs(reset) {
      if (reset) { logOffset = 0; logSearchMode = true; }
      var url = '/api/logs?limit=50&offset='+logOffset;
      if (logSearchMode) {
        var lfrom = document.getElementById('log-from').value;
        var lto = document.getElementById('log-to').value;
        if (lfrom) url += '&date_from='+lfrom.replace(/-/g,'');
        if (lto) url += '&date_to='+lto.replace(/-/g,'');
      }
      try {
        var d = await fetchJSON(url);
        if (!d.items || !d.items.length) { document.getElementById('logs').innerHTML = '(로그 없음)'; document.getElementById('log-paging').innerHTML=''; return; }
        document.getElementById('logs').innerHTML = d.items.map(function(l){
          var dt = l.created_at ? l.created_at.replace('T',' ').slice(0,19) : '';
          return '<div>['+l.level+'] '+dt+' '+l.message+'</div>';
        }).join('');
        var pg = '';
        if (logOffset > 0) pg += '<button class="btn btn-secondary btn-sm" onclick="logOffset-=50;loadLogs()">이전</button>';
        if (d.items.length >= 50) pg += '<button class="btn btn-secondary btn-sm" onclick="logOffset+=50;loadLogs()">다음</button>';
        document.getElementById('log-paging').innerHTML = pg;
      } catch(_) { document.getElementById('logs').innerHTML = '(로그 로드 실패)'; }
    }
    async function loadCycles() {
      try {
        var d = await fetchJSON('/api/cycles');
        if (!d.items || !d.items.length) { document.getElementById('cycles').innerHTML = '<p>아직 완료된 싸이클이 없습니다</p>'; return; }
        var h = '<table><tr><th>Ticker</th><th>기간</th><th>수익금</th><th>수익률</th><th>상세</th><th></th></tr>';
        d.items.forEach(function(c) {
          var cls = c.profit >= 0 ? 'pnl-plus' : 'pnl-minus';
          h += '<tr style="cursor:pointer;" onclick="openCycleDetail('+c.id+')">';
          h += '<td><strong>'+c.ticker+'</strong> <span class="badge badge-cycle">#'+c.cycle_number+'</span></td>';
          h += '<td>'+c.start_date+' ~ '+c.end_date+'</td>';
          h += '<td class="'+cls+'">'+(c.profit>=0?'+':'')+' $'+c.profit.toLocaleString(undefined,{minimumFractionDigits:2})+'</td>';
          h += '<td class="'+cls+'">'+(c.profit_pct>=0?'+':'')+c.profit_pct.toFixed(2)+'%</td>';
          h += '<td><a href="javascript:void(0)" class="btn btn-secondary btn-sm" style="text-decoration:none;display:inline-block;" onclick="event.stopPropagation();openCycleDetail('+c.id+')">상세</a></td>';
          h += '<td><button type="button" class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteCycleReport('+c.id+',\\''+c.ticker+'\\','+c.cycle_number+')">삭제</button></td></tr>';
        });
        if (d.summary) {
          var s = d.summary;
          var scls = s.total_profit >= 0 ? 'pnl-plus' : 'pnl-minus';
          h += '<tr style="border-top:2px solid #30363d;font-weight:bold"><td>합계 ('+s.total_cycles+'회)</td><td></td>';
          h += '<td class="'+scls+'">'+(s.total_profit>=0?'+':'')+' $'+s.total_profit.toLocaleString(undefined,{minimumFractionDigits:2})+'</td>';
          h += '<td class="'+scls+'">'+(s.total_profit_pct>=0?'+':'')+s.total_profit_pct.toFixed(2)+'%</td><td></td><td></td></tr>';
        }
        h += '</table>';
        document.getElementById('cycles').innerHTML = h;
      } catch(_) { document.getElementById('cycles').innerHTML = '<p>리포트 로드 실패</p>'; }
    }
    async function openCycleDetail(cycleId) {
      try {
        var d = await fetchJSON('/api/cycles/'+cycleId+'/trades');
        var c = d.cycle;
        var cls = c.profit >= 0 ? 'pnl-plus' : 'pnl-minus';
        document.getElementById('cycle-detail-title').textContent = c.ticker + ' #' + c.cycle_number + ' 싸이클 상세';
        var sh = '<div style="display:flex;flex-wrap:wrap;gap:16px;font-size:14px;">';
        sh += '<span>기간: <strong>'+c.start_date+' ~ '+c.end_date+'</strong></span>';
        sh += '<span>총매수: <strong class="buy">$'+c.total_buy_amount.toLocaleString(undefined,{minimumFractionDigits:2})+'</strong></span>';
        sh += '<span>총매도: <strong class="sell">$'+c.total_sell_amount.toLocaleString(undefined,{minimumFractionDigits:2})+'</strong></span>';
        sh += '<span>수익: <strong class="'+cls+'">'+(c.profit>=0?'+':'')+'$'+c.profit.toLocaleString(undefined,{minimumFractionDigits:2})+' ('+c.profit_pct.toFixed(2)+'%)</strong></span>';
        sh += '</div>';
        document.getElementById('cycle-detail-summary').innerHTML = sh;
        if (!d.trades || !d.trades.length) {
          document.getElementById('cycle-detail-trades').innerHTML = '<p>거래내역 없음</p>';
        } else {
          var h = '<table><tr><th>일자</th><th>구분</th><th>유형</th><th>단가</th><th>수량</th><th>금액</th></tr>';
          d.trades.forEach(function(t) {
            var tcls = t.side==='buy'?'buy':'sell';
            h += '<tr><td>'+t.trade_date+'</td><td class="'+tcls+'">'+(t.side==='buy'?'매수':'매도')+'</td><td>'+fmtOrdType(t.order_type)+'</td>';
            h += '<td>$'+(t.price||0).toFixed(2)+'</td><td>'+t.qty+'주</td><td>$'+(t.amount||0).toLocaleString(undefined,{minimumFractionDigits:2})+'</td></tr>';
          });
          h += '</table>';
          document.getElementById('cycle-detail-trades').innerHTML = h;
        }
        document.getElementById('cycle-detail-modal').classList.add('show');
      } catch(e) { alert('상세 로드 실패: '+e.message); }
    }
    async function deleteCycleReport(cycleId, ticker, cycleNum) {
      if (!confirm(ticker+' #'+cycleNum+' 성공리포트 1건을 삭제할까요?\\n(포트폴리오 초기화와 무관, 이 항목만 영구 삭제)')) return;
      try {
        var r = await fetch('/api/cycles/'+cycleId, { method: 'DELETE' });
        if (!r.ok) { var txt = await r.text(); alert('삭제 실패:\\n'+txt); return; }
        var d = await r.json();
        alert(d.message || '삭제됨');
        loadCycles();
      } catch(e) { alert('삭제 오류: '+e.message); }
    }
    async function loadTradingMode() {
      try { var d = await fetchJSON('/api/settings'); if (d.trading_mode) document.getElementById('trading-mode').textContent = d.trading_mode; } catch(_){}
    }
    function loadAll() {
      loadAccountSummary().catch(function(){});
      loadPortfolios().catch(function(){});
      loadOrders().catch(function(){});
      loadTrades().catch(function(){});
      loadCycles().catch(function(){});
      loadLogs().catch(function(){});
    }
    function init() {
      document.getElementById('btn-open-settings').addEventListener('click', openSettings);
      document.getElementById('btn-add-portfolio').addEventListener('click', openAddPortfolio);
      document.getElementById('btn-kill-on').addEventListener('click', function(){ killSwitch(true); });
      document.getElementById('btn-kill-off').addEventListener('click', function(){ killSwitch(false); });
      document.getElementById('btn-force-sync').addEventListener('click', forceSync);
      document.getElementById('btn-close-settings').addEventListener('click', closeSettings);
      document.getElementById('btn-close-add-portfolio').addEventListener('click', closeAddPortfolio);
      document.getElementById('btn-close-edit-initial-cost').addEventListener('click', closeEditInitialCost);
      document.getElementById('settings-form').addEventListener('submit', function(e){ saveSettings(e); });
      document.getElementById('add-portfolio-form').addEventListener('submit', function(e){ addPortfolio(e); });
      document.getElementById('edit-initial-cost-form').addEventListener('submit', function(e){ saveInitialCost(e); });
      
      killSwitchStatus();
      loadTradingMode();
      loadAll();
      setInterval(loadAll, 30000);
    }
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', init); } else { init(); }
  </script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """대시보드 페이지 (설정, 포트폴리오, 주문, 로그, Kill Switch)"""
    from core.dashboard_prefix import inject_api_base
    base = request.scope.get("root_path", "") or ""
    html = inject_api_base(_load_dashboard_html(), base)
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store, no-cache"})


if __name__ == "__main__":
    """직접 실행 시 uvicorn 서버 구동. host=0.0.0.0으로 외부 접속 허용"""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
