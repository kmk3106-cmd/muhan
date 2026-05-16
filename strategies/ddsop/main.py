# -*- coding: utf-8 -*-
"""
떨사오팔 v1 - FastAPI 서버 + 스케줄러
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    import pytz
    KST = pytz.timezone("Asia/Seoul")

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, select, desc, func
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL, TRADING_MODE, SERVER_PORT
from .models import init_db, Ticker, Tranche, TradeOrder, Trade, CycleHistory, AppLog, TrancheStatus
from .settings_store import get_settings_for_display, save_settings, get_account_summary
from .worker import run_worker_once, kill_switch_activate, kill_switch_deactivate, is_kill_switch_on, get_us_market_run_time_kst, get_next_worker_run_kst, get_shared_client, refresh_shared_client
from .trading_logic import generate_orders
from core.schedule_map import (
    shift as sched_shift,
    DDSOP_WORKER_MIN_OFFSET,
    DDSOP_PRE_AUTH_LEAD_MIN,
    DDSOP_SYNC_MINUTE,
    DDSOP_SYNC_SECOND,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

def _pre_auth():
    try:
        client, ctac = get_shared_client()
        if client.auth(ctac):
            logger.info("[사전인증] 토큰 발급 완료")
    except Exception as e:
        logger.warning(f"[사전인증] 오류: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DATABASE_URL)
    run_h, run_m = get_us_market_run_time_kst()
    logger.info(f"떨사오팔 스케줄: 매일 {run_h:02d}:{run_m:02d}~{(run_h+5)%24:02d}:{run_m:02d} KST (매시, 미국장 개시 30분 후)")
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    pre_h, pre_m = sched_shift(run_h, run_m, -DDSOP_PRE_AUTH_LEAD_MIN)
    scheduler.add_job(_pre_auth, "cron", hour=pre_h, minute=pre_m, id="pre_auth")
    # 워커: 미국장 개시 30분 후 + 매시 정각 6회 (한 번 주문 제출되면 미체결 유지, 재실행 시 추가 제출 가능)
    # 단일계좌 레인 분리: 무한매수법 워커(:run_m) 와 충돌하지 않도록 +오프셋 분에 실행
    for i in range(6):
        h = (run_h + i) % 24
        wh, wm = sched_shift(h, run_m, DDSOP_WORKER_MIN_OFFSET)
        scheduler.add_job(run_worker_once, "cron", hour=wh, minute=wm, id=f"worker_{i}")

    def _reschedule_worker_for_dst():
        """매일 9시 KST에 워커/사전인증 스케줄 재계산 → 써머타임 변경 시 자동 반영"""
        run_h, run_m = get_us_market_run_time_kst()
        pre_h, pre_m = sched_shift(run_h, run_m, -DDSOP_PRE_AUTH_LEAD_MIN)
        scheduler.reschedule_job("pre_auth", trigger="cron", hour=pre_h, minute=pre_m)
        for i in range(6):
            h = (run_h + i) % 24
            wh, wm = sched_shift(h, run_m, DDSOP_WORKER_MIN_OFFSET)
            try:
                scheduler.reschedule_job(f"worker_{i}", trigger="cron", hour=wh, minute=wm)
            except Exception:
                pass
        logger.info(f"[스케줄 갱신] 워커: {run_h:02d}:{run_m:02d}~{(run_h+5)%24:02d}:{run_m:02d} KST (매시, 써머타임 반영)")

    scheduler.add_job(_reschedule_worker_for_dst, "cron", hour=9, minute=0, id="reschedule_dst")

    # 동기화 전용: 단일계좌 레인 분리를 위해 interval(2분 드리프트) → cron(짝수분 :40초) 전환.
    # 빈도 동일(30회/시), 동작 동일(submit_orders=False), 무한매수법 sync(홀수분 :20초)와 비충돌.
    scheduler.add_job(
        lambda: run_worker_once(submit_orders=False),
        "cron", minute=DDSOP_SYNC_MINUTE, second=DDSOP_SYNC_SECOND, id="sync_only",
        max_instances=1,
    )
    scheduler.start()
    try:
        from .kis_ws import start_ws_ccnl_thread
        start_ws_ccnl_thread()
    except Exception as e:
        logger.warning(f"WebSocket 체결통보 미시작: {e}")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="떨사오팔 v1", lifespan=lifespan)


# ========== Pydantic ==========
class TickerCreate(BaseModel):
    ticker: str
    total_usd: float
    num_tranches: int = 5
    x_pct: float = 3.0
    loss_cut_days: int = 40


# ========== API: 설정 ==========
@app.get("/api/settings")
def api_get_settings():
    return get_settings_for_display(DATABASE_URL)


@app.post("/api/settings")
def api_save_settings(data: dict):
    save_settings(data, DATABASE_URL)
    refresh_shared_client()
    return {"message": "설정 저장 완료"}


# ========== API: Kill Switch ==========
@app.get("/api/kill_switch")
def api_kill_switch():
    return {"active": is_kill_switch_on()}


@app.post("/api/kill_switch")
def api_kill_switch_toggle(activate: bool = True):
    if activate:
        kill_switch_activate()
    else:
        kill_switch_deactivate()
    return {"active": is_kill_switch_on()}


# ========== API: 상태 (다음 워커, KIS API 연결) ==========
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
        client, ctac = get_shared_client()
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


# ========== API: 계좌 요약 ==========
@app.get("/api/account_summary")
def api_account_summary():
    data = get_account_summary(DATABASE_URL)
    if not data:
        return {"stock_evlu": 0, "buy_amt": 0, "cash": 0, "tot_evlu": 0,
                "pnl": 0, "pnl_rt": 0, "exrt": 0, "updated_at": None}
    return {
        "stock_evlu": data.get("stock_evlu", 0),
        "buy_amt": data.get("buy_amt", 0),
        "cash": data.get("cash", 0),
        "tot_evlu": data.get("tot_asst_amt", 0),
        "pnl": data.get("pnl", 0),
        "pnl_rt": data.get("pnl_rt", 0),
        "exrt": data.get("exrt", 0),
        "updated_at": data.get("updated_at"),
    }


# ========== API: 동기화 ==========
@app.post("/api/sync")
def api_sync():
    """동기화만: 체결/싸이클/계좌만 갱신 (주문 제출 없음)"""
    try:
        result = run_worker_once(submit_orders=False)
        return result
    except Exception as e:
        logger.exception(f"동기화 오류: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/run_worker")
def api_run_worker():
    """전체 실행: 동기화 + 주문 제출 (스케줄된 워커와 동일)"""
    try:
        result = run_worker_once(submit_orders=True)
        return result
    except Exception as e:
        logger.exception(f"워커 실행 오류: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ========== API: 종목 (Ticker) CRUD ==========
def _replace_ticker_tranches(session: Session, tk: Ticker, num_tranches: int, total_usd: float) -> None:
    """미체결·체결 삭제 후 트렌치 전부 교체 (삭제 후 재등록 등)."""
    session.execute(
        TradeOrder.__table__.delete().where(
            TradeOrder.tranche_id.in_(
                select(Tranche.id).where(Tranche.ticker_id == tk.id)
            )
        )
    )
    session.execute(
        Trade.__table__.delete().where(
            Trade.tranche_id.in_(
                select(Tranche.id).where(Tranche.ticker_id == tk.id)
            )
        )
    )
    session.execute(Tranche.__table__.delete().where(Tranche.ticker_id == tk.id))
    session.flush()
    amt_per = total_usd / num_tranches
    for i in range(1, num_tranches + 1):
        session.add(Tranche(
            ticker_id=tk.id, tranche_num=i,
            amount_per_tranche=round(amt_per, 2),
        ))


@app.get("/api/tickers")
def api_get_tickers():
    with SessionLocal() as session:
        tickers = session.scalars(select(Ticker).where(Ticker.is_active == True)).all()
        result = []
        for tk in tickers:
            tranches = session.scalars(
                select(Tranche).where(Tranche.ticker_id == tk.id)
            ).all()
            bought = sum(1 for t in tranches if t.status == TrancheStatus.BOUGHT.value)
            result.append({
                "id": tk.id, "ticker": tk.ticker, "total_usd": tk.total_usd,
                "num_tranches": tk.num_tranches, "x_pct": tk.x_pct,
                "loss_cut_days": getattr(tk, "loss_cut_days", 40) or 40,
                "is_active": tk.is_active, "trading_enabled": tk.trading_enabled,
                "seed_reflect_enabled": getattr(tk, "seed_reflect_enabled", False) or False,
                "current_cycle": tk.current_cycle,
                "bought_count": bought, "total_count": len(tranches),
            })
        return result


@app.post("/api/tickers")
def api_add_ticker(data: TickerCreate):
    with SessionLocal() as session:
        sym = data.ticker.upper()
        try:
            from core.ticker_registry import assert_ticker_available, TickerConflict
            assert_ticker_available("ddsop", sym)
        except TickerConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        existing = session.scalar(select(Ticker).where(Ticker.ticker == sym))
        if existing and existing.is_active:
            raise HTTPException(status_code=400, detail=f"{sym} 이미 등록됨")
        if existing and not existing.is_active:
            tk = existing
            tk.is_active = True
            tk.trading_enabled = False
            tk.total_usd = data.total_usd
            tk.num_tranches = data.num_tranches
            tk.x_pct = data.x_pct
            tk.loss_cut_days = max(1, min(365, data.loss_cut_days))
            tk.current_cycle = 1
            tk.seed_reflect_enabled = False
            _replace_ticker_tranches(session, tk, data.num_tranches, data.total_usd)
            session.commit()
            _cached_orders["data"] = []
            _cached_orders["ts"] = 0
            return {"message": f"{sym} 다시 등록 완료 (트렌치 {data.num_tranches}개)"}
        tk = Ticker(
            ticker=sym, total_usd=data.total_usd,
            num_tranches=data.num_tranches, x_pct=data.x_pct,
            loss_cut_days=max(1, min(365, data.loss_cut_days)),
        )
        session.add(tk)
        session.flush()
        amt_per = data.total_usd / data.num_tranches
        for i in range(1, data.num_tranches + 1):
            session.add(Tranche(
                ticker_id=tk.id, tranche_num=i,
                amount_per_tranche=round(amt_per, 2),
            ))
        session.commit()
        return {"message": f"{sym} 등록 완료 (트렌치 {data.num_tranches}개)"}


@app.delete("/api/tickers/{ticker_id}")
def api_delete_ticker(ticker_id: int):
    with SessionLocal() as session:
        tk = session.get(Ticker, ticker_id)
        if not tk:
            raise HTTPException(status_code=404, detail="종목 없음")
        # "삭제"는 소프트 삭제로 처리: 성공리포트(CycleHistory)는 영구 보존
        tk.is_active = False
        tk.trading_enabled = False
        session.commit()
        return {"message": f"{tk.ticker} 삭제 완료 (성공리포트는 유지됩니다)"}


def _cancel_pending_orders_for_ticker(ticker: str) -> int:
    """해당 종목 미체결 주문 일괄 취소. 취소한 건수 반환."""
    try:
        client, ctac = get_shared_client()
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
                    ctac_tlno=ctac,
                )
                if res.get("rt_cd") == "0":
                    cancelled += 1
            except Exception:
                pass
        return cancelled
    except Exception:
        return 0


@app.patch("/api/tickers/{ticker_id}/seed_reflect")
def api_toggle_seed_reflect(ticker_id: int):
    """종목별 씨드반영 ON/OFF 토글"""
    with SessionLocal() as session:
        tk = session.get(Ticker, ticker_id)
        if not tk:
            raise HTTPException(status_code=404, detail="종목 없음")
        tk.seed_reflect_enabled = not (getattr(tk, "seed_reflect_enabled", False) or False)
        session.commit()
        return {"seed_reflect_enabled": tk.seed_reflect_enabled, "ticker": tk.ticker}


@app.patch("/api/tickers/{ticker_id}/trading")
def api_toggle_trading(ticker_id: int):
    """종목 진행 ON/OFF 토글. OFF 시 해당 종목 미체결 자동 취소"""
    with SessionLocal() as session:
        tk = session.get(Ticker, ticker_id)
        if not tk:
            raise HTTPException(status_code=404, detail="종목 없음")
        was_enabled = tk.trading_enabled
        tk.trading_enabled = not tk.trading_enabled
        session.commit()
        cancelled = 0
        if was_enabled and not tk.trading_enabled:
            cancelled = _cancel_pending_orders_for_ticker(tk.ticker)
            if cancelled:
                session.add(AppLog(level="WARNING", message=f"종목 OFF로 인한 미체결 취소: {tk.ticker} {cancelled}건"))
                session.commit()
        result = {"trading_enabled": tk.trading_enabled, "ticker": tk.ticker}
        if cancelled:
            result["message"] = f"미체결 {cancelled}건 자동 취소"
        return result


@app.post("/api/tickers/{ticker_id}/reset")
def api_reset_ticker(ticker_id: int):
    with SessionLocal() as session:
        tk = session.get(Ticker, ticker_id)
        if not tk:
            raise HTTPException(status_code=404, detail="종목 없음")
        session.execute(
            TradeOrder.__table__.delete().where(
                TradeOrder.tranche_id.in_(
                    select(Tranche.id).where(Tranche.ticker_id == tk.id)
                )
            )
        )
        session.execute(
            Trade.__table__.delete().where(
                Trade.tranche_id.in_(
                    select(Tranche.id).where(Tranche.ticker_id == tk.id)
                )
            )
        )
        tranches = session.scalars(select(Tranche).where(Tranche.ticker_id == tk.id)).all()
        for t in tranches:
            t.status = TrancheStatus.IDLE.value
            t.avg_price = 0.0
            t.qty = 0
            t.buy_price = 0.0
            t.buy_date = ""
            t.days_held = 0
            t.cycle_number = 1
        tk.current_cycle = 1
        session.commit()
        # 오늘주문 캐시 무효화 (초기화 후 즉시 반영)
        _cached_orders["data"] = []
        _cached_orders["ts"] = 0
        return {"message": f"{tk.ticker} 초기화 완료 (성공리포트는 유지됩니다)"}


# ========== API: 트렌치 상태 ==========
@app.get("/api/tickers/{ticker_id}/tranches")
def api_get_tranches(ticker_id: int):
    with SessionLocal() as session:
        tk = session.get(Ticker, ticker_id)
        if not tk:
            raise HTTPException(status_code=404, detail="종목 없음")
        if not tk.is_active:
            raise HTTPException(status_code=404, detail="종목 없음")
        tranches = session.scalars(
            select(Tranche).where(Tranche.ticker_id == tk.id).order_by(Tranche.tranche_num)
        ).all()
        return {
            "ticker": tk.ticker,
            "tranches": [{
                "id": t.id, "tranche_num": t.tranche_num, "status": t.status,
                "avg_price": t.avg_price, "qty": t.qty, "buy_date": t.buy_date,
                "buy_price": t.buy_price, "days_held": t.days_held,
                "amount_per_tranche": t.amount_per_tranche,
                "pnl": 0.0,
            } for t in tranches],
        }


def _normalize_odno(s: str) -> str:
    """주문번호 정규화 (앞 0 제거) - KIS odno 형식 차이 대응"""
    t = str(s or "").strip().lstrip("0")
    return t if t else "0"


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
    """컬럼명에 패턴이 포함된 첫 번째 유효값 반환. patterns는 대소문자무관 부분문자열."""
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
        v = _find_col_val(row, "ord_dvsn", "ord_dvsn", "dvsn")
    s = str(v).strip().upper() if v else "00"
    if s in ("LOC", "34"):
        return "34"
    if s in ("MOC", "33"):
        return "33"
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


# ========== API: 미체결 주문 ==========
@app.get("/api/orders/pending")
def api_orders_pending():
    """KIS 미체결 주문 실시간 조회 (등록된 종목만). DB 매칭 시 트렌치/싸이클/구분 정확 표시."""
    try:
        with SessionLocal() as session:
            registered = {tk.ticker for tk in session.scalars(select(Ticker).where(Ticker.is_active == True)).all()}
            # kis_order_no -> {tranche_num, cycle_number, side, order_type, price} (우리 DB 기준)
            order_map = {}
            db_orders = session.execute(
                select(TradeOrder, Tranche).join(Tranche, TradeOrder.tranche_id == Tranche.id).where(
                    TradeOrder.status.in_(["pending", "submitted"]),
                    TradeOrder.kis_order_no != "",
                )
            ).all()
            order_map_by_attr = {}  # (ticker, side, price, qty) -> info (odno 매칭 실패 시 폴백)
            for o, tr in db_orders:
                ono = str(o.kis_order_no or "").strip()
                side_kr = "매수" if o.side == "buy" else "매도"
                price_f = float(o.price or 0)
                qty_i = int(o.qty or 0)
                info = {
                    "tranche_num": tr.tranche_num,
                    "cycle_number": tr.cycle_number or 1,
                    "side": side_kr,
                    "order_type": o.order_type,
                    "price": price_f,
                }
                if ono:
                    order_map[ono] = info
                    order_map[_normalize_odno(ono)] = info
                order_map_by_attr[(o.ticker, side_kr, round(price_f, 2), qty_i)] = info
        if not registered:
            return {"items": [], "error": ""}
        client, ctac = get_shared_client()
        if not client.auth(ctac):
            return {"items": [], "error": "KIS 인증 실패"}
        # NASD, NYSE, AMEX 각각 조회 후 병합 (중복 제거)
        nccs_parts = []
        for ovrs in ["NASD", "NYSE", "AMEX"]:
            df = client.inquire_nccs(ovrs_excg_cd=ovrs, ctac_tlno=ctac)
            if not df.empty:
                nccs_parts.append(df)
        df = pd.concat(nccs_parts, ignore_index=True) if nccs_parts else client.inquire_nccs(ctac_tlno=ctac)
        if df.empty:
            return {"items": [], "error": ""}
        def _extract_odno(r):
            v = _row_val(r, "odno", "ORGN_ODNO", "ODNO", default="")
            return str(v or "").strip()
        df["_odno_norm"] = df.apply(lambda r: _normalize_odno(_extract_odno(r)), axis=1)
        df = df.drop_duplicates(subset=["_odno_norm"], keep="first")
        items = []
        for _, row in df.iterrows():
            pdno = str(_row_val(row, "pdno", "PDNO", default="")).strip()
            if pdno not in registered:
                continue
            odno = str(_row_val(row, "odno", "ORGN_ODNO", "ODNO", default="")).strip()
            db_info = (order_map.get(odno) or order_map.get(_normalize_odno(odno))) if odno else None
            if not db_info:
                raw_price = _row_val(row, "ord_unpr", "ovrs_ord_unpr", "ORD_UNPR", "OVRS_ORD_UNPR",
                                     "ord_prpr", "ORD_PRPR", "ft_ord_unpr", "FT_ORD_UNPR", "ft_ord_unpr3", default="")
                try:
                    price_f = float(raw_price) if raw_price and str(raw_price).strip() not in ("", "-") else 0.0
                except (TypeError, ValueError):
                    price_f = 0.0
                qty_raw = _row_val(row, "nccs_qty", "NCCS_QTY", "ord_qty", "ORD_QTY", default="0")
                qty_i = int(float(qty_raw)) if qty_raw else 0
                side_k = _parse_kis_side(row)
                db_info = order_map_by_attr.get((pdno, side_k, round(price_f, 2), qty_i))
            if db_info:
                side = db_info["side"]
                order_type = db_info["order_type"]
                price = db_info["price"]
                prefix = "LOC " if order_type == "LOC" else ("MOC " if order_type == "MOC" else "지정가 ")
                side_label = prefix + side
                tranche_num = db_info["tranche_num"]
                cycle_number = db_info["cycle_number"]
            else:
                side = _parse_kis_side(row)
                ord_dvsn = _parse_kis_ord_dvsn(row)
                prefix = "LOC " if ord_dvsn == "34" else ("MOC " if ord_dvsn == "33" else ("지정가 " if ord_dvsn == "00" else ""))
                side_label = prefix + side
                raw_price = _row_val(row, "ord_unpr", "ovrs_ord_unpr", "ORD_UNPR", "OVRS_ORD_UNPR",
                                     "ord_prpr", "ORD_PRPR", "ft_ord_unpr", "FT_ORD_UNPR", "ft_ord_unpr3", default="")
                try:
                    price_f = float(raw_price) if raw_price and str(raw_price).strip() not in ("", "-") else 0.0
                except (TypeError, ValueError):
                    price_f = 0.0
                price = price_f
                tranche_num = None
                cycle_number = None
            qty_raw = _row_val(row, "nccs_qty", "NCCS_QTY", "ord_qty", "ORD_QTY", default="0")
            qty = int(float(qty_raw)) if qty_raw else 0
            items.append({
                "ticker": pdno,
                "side": side,
                "side_label": side_label,
                "price": price,
                "ord_dvsn": _row_val(row, "ord_dvsn", "ORD_DVSN", default=""),
                "qty": qty,
                "order_no": odno,
                "ord_dt": str(_row_val(row, "ord_dt", "ORD_DT", default="")),
                "ord_tmd": str(_row_val(row, "ord_tmd", "ORD_TMD", default="")),
                "tranche_num": tranche_num,
                "cycle_number": cycle_number,
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
    """미체결 주문 일괄 취소 (대시보드 '주문취소' 버튼). 호출 시 로그 기록."""
    try:
        if body.items:
            msg = "미체결 주문 취소 API 호출: " + ", ".join(f"{x.ticker} #{x.order_no}" for x in body.items)
            with SessionLocal() as session:
                session.add(AppLog(level="WARNING", message=msg))
                session.commit()
        client, ctac = get_shared_client()
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


# ========== API: 오늘 주문 ==========
_cached_orders = {"data": [], "ts": 0}
_ORDER_CACHE_SEC = 120


@app.get("/api/orders/today")
def api_orders_today():
    import time as _t
    now = _t.time()
    if _cached_orders["data"] and (now - _cached_orders["ts"]) < _ORDER_CACHE_SEC:
        return _cached_orders["data"]

    acct = get_account_summary(DATABASE_URL)
    actual_cash = float(acct.get("cash", 0) or 0) if acct else 0.0

    import time as _tmod
    result = []
    with SessionLocal() as session:
        tickers = session.scalars(
            select(Ticker).where(Ticker.is_active == True, Ticker.trading_enabled == True)
        ).all()
        for i, tk in enumerate(tickers):
            if i > 0:
                _tmod.sleep(0.5)
            tranches = session.scalars(
                select(Tranche).where(Tranche.ticker_id == tk.id)
            ).all()
            try:
                client, ctac = get_shared_client()
                if client.auth(ctac):
                    prev_close, price_src = client.inquire_prev_close(tk.ticker, ctac_tlno=ctac)
                else:
                    prev_close, price_src = 0, "error"
            except Exception:
                prev_close, price_src = 0, "error"
            if prev_close <= 0:
                try:
                    import yfinance as yf
                    t = yf.Ticker(tk.ticker)
                    info = t.info
                    p = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
                    if p is not None:
                        fp = float(p)
                        if fp > 0:
                            prev_close, price_src = fp, "Yahoo"
                except Exception:
                    pass

            today_str = datetime.now().strftime("%Y%m%d")
            from .worker import _count_trading_days
            for t in tranches:
                if t.status == "BOUGHT" and t.buy_date:
                    try:
                        t.days_held = _count_trading_days(t.buy_date, today_str)
                    except Exception:
                        pass
            orders = generate_orders(tk, tranches, prev_close, today_str, actual_cash=actual_cash)
            for o in orders:
                src_label = "Yahoo" if price_src == "Yahoo" else f"KIS {price_src}"
                desc_display = o.desc + f" [{src_label}]" if price_src and prev_close > 0 else o.desc
                result.append({
                    "ticker": o.ticker, "tranche_num": o.tranche_num, "cycle_number": getattr(o, "cycle_number", 1) or 1,
                    "side": o.side, "order_type": o.order_type,
                    "price": o.price, "qty": o.qty, "desc": desc_display,
                    "amount": round(o.price * o.qty, 2) if o.price > 0 else 0,
                })
    _cached_orders["data"] = result
    _cached_orders["ts"] = now
    return result


# ========== API: 거래내역 ==========
@app.get("/api/trades")
def api_trades(limit: int = 50, offset: int = 0, sort: str = "desc",
               date_from: str = "", date_to: str = ""):
    with SessionLocal() as session:
        active_tickers = session.scalars(
            select(Ticker).where(Ticker.is_active == True)
        ).all()
        active_ids = {tk.id for tk in active_tickers}
        active_tranche_ids = set()
        for tk in active_tickers:
            tranches = session.scalars(
                select(Tranche).where(Tranche.ticker_id == tk.id)
            ).all()
            active_tranche_ids.update(t.id for t in tranches)

        base = select(Trade).where(Trade.tranche_id.in_(active_tranche_ids)) if active_tranche_ids else select(Trade).where(False)
        count_q = select(func.count(Trade.id)).where(Trade.tranche_id.in_(active_tranche_ids)) if active_tranche_ids else select(func.count(Trade.id)).where(False)

        if date_from:
            base = base.where(Trade.trade_date >= date_from)
            count_q = count_q.where(Trade.trade_date >= date_from)
        if date_to:
            base = base.where(Trade.trade_date <= date_to)
            count_q = count_q.where(Trade.trade_date <= date_to)

        total = session.scalar(count_q) or 0
        order_col = Trade.trade_date.asc() if sort == "asc" else Trade.trade_date.desc()
        trades = session.scalars(base.order_by(order_col, Trade.id.desc()).offset(offset).limit(limit)).all()

        return {
            "items": [{
                "id": t.id, "ticker": t.ticker,
                "cycle_number": getattr(t, "cycle_number", 1) or 1,
                "tranche_num": t.tranche_num,
                "side": t.side, "order_type": t.order_type,
                "price": t.price, "qty": t.qty, "amount": t.amount,
                "trade_date": t.trade_date,
            } for t in trades],
            "total_count": total,
        }


@app.delete("/api/trades/{trade_id}")
def api_trade_delete(trade_id: int):
    """체결 거래내역 1건 삭제 (활성 티커 소유만)"""
    with SessionLocal() as session:
        active_tickers = session.scalars(select(Ticker).where(Ticker.is_active == True)).all()
        active_tranche_ids = set()
        for tk in active_tickers:
            for tr in session.scalars(select(Tranche).where(Tranche.ticker_id == tk.id)).all():
                active_tranche_ids.add(tr.id)
        trade = session.get(Trade, trade_id)
        if not trade:
            raise HTTPException(404, "거래내역 없음")
        if trade.tranche_id not in active_tranche_ids:
            raise HTTPException(403, "해당 거래를 삭제할 수 없습니다.")
        session.delete(trade)
        session.commit()
    return {"success": True, "id": trade_id}


@app.get("/api/trades/export")
def api_trades_export(date_from: str = "", date_to: str = ""):
    import csv
    import io
    from fastapi.responses import StreamingResponse

    with SessionLocal() as session:
        active_tickers = session.scalars(
            select(Ticker).where(Ticker.is_active == True)
        ).all()
        active_tranche_ids = set()
        for tk in active_tickers:
            trs = session.scalars(select(Tranche).where(Tranche.ticker_id == tk.id)).all()
            active_tranche_ids.update(t.id for t in trs)
        base = select(Trade).where(Trade.tranche_id.in_(active_tranche_ids)) if active_tranche_ids else select(Trade).where(False)
        if date_from:
            base = base.where(Trade.trade_date >= date_from)
        if date_to:
            base = base.where(Trade.trade_date <= date_to)
        trades = session.scalars(base.order_by(Trade.trade_date.desc())).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["일자", "Ticker", "싸이클", "트렌치", "구분", "유형", "가격", "수량", "금액"])
    for t in trades:
        cy = getattr(t, "cycle_number", 1) or 1
        writer.writerow([t.trade_date, t.ticker, f"C{cy}", f"T{t.tranche_num}",
                         "매수" if t.side == "buy" else "매도", t.order_type,
                         t.price, t.qty, t.amount])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=ddsop_trades_{datetime.now():%Y%m%d}.csv"},
    )


# ========== API: 싸이클 이력 ==========
@app.get("/api/cycles")
def api_cycles():
    with SessionLocal() as session:
        cycles = session.scalars(
            select(CycleHistory).order_by(CycleHistory.id.desc())
        ).all()
        items = [{
            "id": c.id, "ticker": c.ticker, "cycle_number": c.cycle_number,
            "start_date": c.start_date, "end_date": c.end_date,
            "total_buy_amount": c.total_buy_amount, "total_sell_amount": c.total_sell_amount,
            "profit": c.profit, "profit_pct": c.profit_pct,
        } for c in cycles]
        total_profit = sum(c.profit for c in cycles)
        total_buy = sum(c.total_buy_amount for c in cycles)
        total_pct = round((total_profit / total_buy * 100) if total_buy > 0 else 0, 2)
        return {
            "items": items,
            "summary": {
                "total_cycles": len(items),
                "total_profit": round(total_profit, 2),
                "total_profit_pct": total_pct,
            },
        }


@app.delete("/api/cycles/{cycle_id}")
def api_delete_cycle(cycle_id: int):
    """성공리포트(싸이클 이력) 1건 수동 삭제 — 종목 초기화와 무관"""
    with SessionLocal() as session:
        c = session.get(CycleHistory, cycle_id)
        if not c:
            raise HTTPException(status_code=404, detail="싸이클 이력 없음")
        session.delete(c)
        session.commit()
        return {"message": "성공리포트 1건이 삭제되었습니다"}


@app.post("/api/cycles/dedupe")
def api_cycles_dedupe():
    """(ticker_id, cycle_number)별 중복 CycleHistory 제거 - 최초 1건만 유지"""
    from collections import defaultdict
    with SessionLocal() as session:
        rows = session.execute(
            select(CycleHistory.id, CycleHistory.ticker_id, CycleHistory.cycle_number)
        ).fetchall()
        keep_per_key = defaultdict(list)
        for id_, tid, cn in rows:
            keep_per_key[(tid, cn)].append(id_)
        keep_ids = {min(ids) for ids in keep_per_key.values()}
        delete_ids = [r[0] for r in rows if r[0] not in keep_ids]
        if delete_ids:
            session.execute(CycleHistory.__table__.delete().where(CycleHistory.id.in_(delete_ids)))
            session.commit()
        return {"deleted": len(delete_ids), "kept": len(keep_ids)}


@app.get("/api/cycles/{cycle_id}/trades")
def api_cycle_trades(cycle_id: int):
    with SessionLocal() as session:
        c = session.get(CycleHistory, cycle_id)
        if not c:
            raise HTTPException(status_code=404, detail="싸이클 없음")
        trades = session.scalars(
            select(Trade).where(
                Trade.ticker == c.ticker,
                Trade.trade_date >= c.start_date,
                Trade.trade_date <= c.end_date,
            ).order_by(Trade.trade_date)
        ).all()
        return {
            "cycle": {
                "ticker": c.ticker, "cycle_number": c.cycle_number,
                "start_date": c.start_date, "end_date": c.end_date,
                "total_buy_amount": c.total_buy_amount, "total_sell_amount": c.total_sell_amount,
                "profit": c.profit, "profit_pct": c.profit_pct,
            },
            "trades": [{
                "trade_date": t.trade_date, "side": t.side, "order_type": t.order_type,
                "price": t.price, "qty": t.qty, "amount": t.amount,
                "cycle_number": getattr(t, "cycle_number", 1) or 1,
                "tranche_num": t.tranche_num,
            } for t in trades],
        }


# ========== API: 로그 ==========
@app.get("/api/logs")
def api_logs(limit: int = 50, offset: int = 0, date_from: str = "", date_to: str = ""):
    with SessionLocal() as session:
        base = select(AppLog)
        if date_from:
            base = base.where(AppLog.created_at >= datetime.strptime(date_from, "%Y%m%d"))
        if date_to:
            base = base.where(AppLog.created_at <= datetime.strptime(date_to, "%Y%m%d") + timedelta(days=1))
        logs = session.scalars(base.order_by(AppLog.id.desc()).offset(offset).limit(limit)).all()
        items = []
        for l in logs:
            dt = l.created_at
            if dt:
                if dt.tzinfo is None:
                    dt = datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
                                  tzinfo=timezone.utc).astimezone(KST)
                else:
                    dt = dt.astimezone(KST)
                created_at = dt.strftime("%Y-%m-%d %H:%M:%S") + " KST"
            else:
                created_at = ""
            items.append({"id": l.id, "level": l.level, "message": l.message, "created_at": created_at})
        return {"items": items}


# ========== 대시보드 ==========
_DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


def _load_dashboard_html() -> str:
    content = _DASHBOARD_PATH.read_text(encoding="utf-8")
    return content.replace("__TRADING_MODE__", TRADING_MODE)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=_load_dashboard_html(), headers={"Cache-Control": "no-store, no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
