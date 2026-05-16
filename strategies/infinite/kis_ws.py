# -*- coding: utf-8 -*-
"""
KIS WebSocket - 해외주식 실시간 체결통보 (H0GSCNI0)
체결 발생 시 즉시 Trade 테이블에 기록
"""
import asyncio
import json
import logging
from base64 import b64decode
from datetime import datetime
from io import StringIO

import pandas as pd

logger = logging.getLogger(__name__)


def _aes_decrypt(key: str, iv: str, cipher_text: str) -> str:
    """AES-CBC Base64 복호화"""
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
        return bytes.decode(unpad(cipher.decrypt(b64decode(cipher_text)), AES.block_size))
    except Exception as e:
        logger.debug(f"[WS] 복호화 실패: {e}")
        return ""

# ORD_DVSN 매핑 (해외주식)
_ORD_DVSN_MAP = {
    "00": "LIMIT", "32": "LOO", "33": "MOC", "34": "LOC",
}

# ccnl_notice 컬럼 (해외주식 실시간체결통보)
_CCNL_COLUMNS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS",
    "ODER_KIND2", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR", "STCK_CNTG_HOUR",
    "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY", "ACNT_NAME",
    "CNTG_ISNM", "ODER_COND", "DEBT_GB", "DEBT_DATE", "START_TM", "END_TM",
    "TM_DIV_TP", "CNTG_UNPR12",
]


def _get_approval_key(cfg: dict) -> str | None:
    """WebSocket 접속키 발급 (REST /oauth2/Approval)"""
    import requests
    trading_mode = cfg.get("trading_mode", "demo")
    if trading_mode == "real":
        url = (cfg.get("prod") or "https://openapi.koreainvestment.com:9443") + "/oauth2/Approval"
        app_key = cfg.get("my_app", "")
        app_sec = cfg.get("my_sec", "")
    else:
        url = (cfg.get("vps") or "https://openapivts.koreainvestment.com:29443") + "/oauth2/Approval"
        app_key = cfg.get("paper_app", "")
        app_sec = cfg.get("paper_sec", "")
    if not app_key or not app_sec:
        logger.warning("[WS] 앱키/시크릿 없음 - WebSocket 체결통보 건너뜀")
        return None
    try:
        resp = requests.post(
            url,
            json={"grant_type": "client_credentials"},
            headers={
                "Content-Type": "application/json",
                "Accept": "text/plain",
                "charset": "UTF-8",
                "User-Agent": cfg.get("my_agent", "Mozilla/5.0"),
                "appkey": app_key,
                "appsecret": app_sec,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"[WS] Approval 실패: {resp.status_code} {resp.text[:200]}")
            return None
        data = resp.json()
        return data.get("approval_key") or None
    except Exception as e:
        logger.warning(f"[WS] Approval 오류: {e}")
        return None


def _handle_ccnl_message(df: pd.DataFrame, cfg: dict) -> None:
    """체결통보 메시지 파싱 → Trade 테이블 저장"""
    if df is None or df.empty:
        return
    from .config import DATABASE_URL
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from .models import Trade, Portfolio, Order, init_db

    init_db(DATABASE_URL)
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)

    for _, row in df.iterrows():
        try:
            odno = str(row.get("ODER_NO", "") or "").strip()
            if not odno:
                continue

            ticker = str(row.get("STCK_SHRN_ISCD", "") or "").strip()
            qty_val = row.get("CNTG_QTY", 0)
            price_val = row.get("CNTG_UNPR", 0) or row.get("CNTG_UNPR12", 0)
            try:
                qty = int(float(qty_val))
                price = float(price_val)
            except (TypeError, ValueError):
                continue
            if qty <= 0 or price <= 0:
                continue

            seln = str(row.get("SELN_BYOV_CLS", "") or "")
            side = "buy" if seln in ("02", "2") else "sell"
            ord_kind = str(row.get("ODER_KIND2", "") or "00")
            order_type = _ORD_DVSN_MAP.get(ord_kind, "LIMIT")

            cng_hour = str(row.get("STCK_CNTG_HOUR", "") or "")
            trade_date = cng_hour[:8] if len(cng_hour) >= 8 else datetime.now().strftime("%Y%m%d")
            amount = round(price * qty, 2)

            with Session() as session:
                pf = session.scalar(select(Portfolio).where(
                    Portfolio.ticker == ticker,
                    Portfolio.is_active == True,
                ))
                if not pf:
                    logger.debug(f"[WS] 포트폴리오 없음: {ticker}, odno={odno}")
                    continue

                existing = session.scalar(
                    select(Trade).where(Trade.odno == odno, Trade.portfolio_id == pf.id)
                )
                if existing:
                    continue

                # Order 테이블에서 odno로 주문가 조회 (LOC 등 체결가≠주문가일 때 표시용)
                ord_row = session.scalar(select(Order).where(
                    Order.odno == odno, Order.portfolio_id == pf.id
                ))
                order_price_val = float(ord_row.price) if ord_row and ord_row.price else None

                from .worker import record_trade
                record_trade(
                    session, pf.id, ord_row.id if ord_row else None, trade_date, side, order_type,
                    price, qty, amount, odno, order_price=order_price_val,
                )
                logger.info(f"[WS] 체결 기록: {ticker} {side} {qty}주 @ ${price:.2f} odno={odno}")

        except Exception as e:
            logger.exception(f"[WS] 체결 처리 오류: {e}")


async def _run_ws_ccnl(cfg: dict):
    """WebSocket 체결통보 구독 실행"""
    import websockets

    approval_key = _get_approval_key(cfg)
    if not approval_key:
        return

    htsid = (cfg.get("my_htsid") or "kmk3106").strip()
    trading_mode = cfg.get("trading_mode", "demo")
    tr_id = "H0GSCNI0" if trading_mode == "real" else "H0GSCNI9"
    ws_url = (cfg.get("ops") or "ws://ops.koreainvestment.com:21000") if trading_mode == "real" else (cfg.get("vops") or "ws://ops.koreainvestment.com:31000")
    ws_url = ws_url.rstrip("/") + "/tryitout"

    msg = {
        "header": {
            "approval_key": approval_key,
            "tr_type": "1",
            "custtype": "P",
        },
        "body": {
            "input": {
                "tr_id": tr_id,
                "tr_key": htsid,
            }
        }
    }

    data_map = {tr_id: {"columns": _CCNL_COLUMNS, "encrypt": None, "key": None, "iv": None}}

    async def on_message(raw: str):
        try:
            if not raw or len(raw) < 2:
                return None
            if raw[0] in ("0", "1") and "|" in raw:
                parts = raw.split("|")
                if len(parts) < 4:
                    return None
                tid = parts[1]
                data_str = parts[3]
                dm = data_map.get(tid, {})
                if dm.get("encrypt") == "Y" and dm.get("key") and dm.get("iv"):
                    data_str = _aes_decrypt(dm["key"], dm["iv"], data_str)
                cols = dm.get("columns", _CCNL_COLUMNS)
                if cols and data_str:
                    df = pd.read_csv(StringIO(data_str), header=None, sep="^", names=cols, dtype=object)
                    _handle_ccnl_message(df, cfg)
            else:
                r = json.loads(raw)
                h = r.get("header", {})
                tid = h.get("tr_id")
                if tid and tid != "PINGPONG":
                    b = r.get("body", {})
                    out = (b or {}).get("output", {})
                    if out:
                        data_map[tid] = {
                            "columns": data_map.get(tid, {}).get("columns", _CCNL_COLUMNS),
                            "encrypt": out.get("encrypt"),
                            "key": out.get("key"),
                            "iv": out.get("iv"),
                        }
                if h.get("tr_id") == "PINGPONG":
                    return raw
        except Exception as e:
            logger.debug(f"[WS] 메시지 처리: {e}")
        return None

    retries = 0
    max_retries = 10

    while retries < max_retries:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                await ws.send(json.dumps(msg))
                logger.info(f"[WS] 체결통보 구독 시작: HTS ID={htsid}, tr_id={tr_id}")

                async for raw in ws:
                    if raw is None:
                        continue
                    pong = await on_message(raw)
                    if pong:
                        await ws.send(pong)
        except asyncio.CancelledError:
            break
        except Exception as e:
            retries += 1
            logger.warning(f"[WS] 연결 끊김 (재시도 {retries}/{max_retries}): {e}")
            await asyncio.sleep(min(30, retries * 5))


def start_ws_ccnl_thread():
    """WebSocket 체결통보 백그라운드 스레드 시작"""
    import threading

    def _run():
        from .settings_store import get_kis_settings
        from .config import DATABASE_URL
        cfg = get_kis_settings(DATABASE_URL)
        if not cfg or not (cfg.get("my_app") or cfg.get("paper_app")):
            logger.info("[WS] KIS 설정 없음 - 체결통보 WebSocket 미시작")
            return
        try:
            asyncio.run(_run_ws_ccnl(cfg))
        except Exception as e:
            logger.exception(f"[WS] 종료: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info("[WS] 체결통보 WebSocket 스레드 시작")
