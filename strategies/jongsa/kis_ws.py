# -*- coding: utf-8 -*-
"""
종사종팔 - KIS WebSocket 해외주식 실시간 체결통보 (H0GSCNI0)
체결 발생 시 Order 매칭 후 Trade 기록 + Tranche 상태 업데이트
"""
import asyncio
import json
import logging
from base64 import b64decode
from datetime import datetime, timezone
from io import StringIO

import pandas as pd

logger = logging.getLogger(__name__)

_ORD_DVSN_MAP = {"00": "LIMIT", "32": "LOO", "33": "MOC", "34": "LOC"}

_CCNL_COLUMNS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS",
    "ODER_KIND2", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR", "STCK_CNTG_HOUR",
    "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY", "ACNT_NAME",
    "CNTG_ISNM", "ODER_COND", "DEBT_GB", "DEBT_DATE", "START_TM", "END_TM",
    "TM_DIV_TP", "CNTG_UNPR12",
]


def _aes_decrypt(key: str, iv: str, cipher_text: str) -> str:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
        return bytes.decode(unpad(cipher.decrypt(b64decode(cipher_text)), AES.block_size))
    except Exception as e:
        logger.debug(f"[WS] 복호화 실패: {e}")
        return ""


def _get_approval_key(cfg: dict) -> str | None:
    import requests
    trading_mode = cfg.get("trading_mode", "demo")
    if trading_mode == "real":
        url = (cfg.get("prod") or "https://openapi.koreainvestment.com:9443") + "/oauth2/Approval"
        app_key, app_sec = cfg.get("my_app", ""), cfg.get("my_sec", "")
    else:
        url = (cfg.get("vps") or "https://openapivts.koreainvestment.com:29443") + "/oauth2/Approval"
        app_key, app_sec = cfg.get("paper_app", ""), cfg.get("paper_sec", "")
    if not app_key or not app_sec:
        logger.warning("[WS] 앱키/시크릿 없음 - WebSocket 체결통보 건너뜀")
        return None
    try:
        resp = requests.post(
            url, json={"grant_type": "client_credentials"},
            headers={
                "Content-Type": "application/json", "Accept": "text/plain", "charset": "UTF-8",
                "User-Agent": cfg.get("my_agent", "Mozilla/5.0"),
                "appkey": app_key, "appsecret": app_sec,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("approval_key")
    except Exception as e:
        logger.warning(f"[WS] Approval 오류: {e}")
        return None


def _handle_ccnl_message(df: pd.DataFrame, cfg: dict) -> None:
    """체결통보 → Order 매칭 → Trade 기록 + Tranche 업데이트"""
    if df is None or df.empty:
        return
    from .config import DATABASE_URL
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from .models import Ticker, Tranche, TradeOrder, Trade, TrancheStatus, AppLog, init_db

    init_db(DATABASE_URL)
    Session = sessionmaker(bind=create_engine(DATABASE_URL))

    for _, row in df.iterrows():
        try:
            rfus = str(row.get("RFUS_YN", "") or "").strip().upper()
            if rfus in ("Y", "1"):
                logger.debug(f"[WS] 거부 통보 스킵: odno={row.get('ODER_NO')}")
                continue

            cng_yn = str(row.get("CNTG_YN", "") or "").strip().upper()
            if cng_yn and cng_yn not in ("Y", "1"):
                logger.debug(f"[WS] 체결 아님 스킵: odno={row.get('ODER_NO')} CNTG_YN={cng_yn}")
                continue

            odno = str(row.get("ODER_NO", "") or "").strip()
            if not odno:
                continue

            ticker_sym = str(row.get("STCK_SHRN_ISCD", "") or "").strip()
            try:
                qty = int(float(row.get("CNTG_QTY", 0) or 0))
                price = float(row.get("CNTG_UNPR", 0) or row.get("CNTG_UNPR12", 0) or 0)
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
                order = session.scalar(
                    select(TradeOrder).where(
                        TradeOrder.kis_order_no == odno,
                        TradeOrder.status.in_(["pending", "submitted"]),
                    )
                )
                if not order:
                    logger.debug(f"[WS] 매칭 주문 없음: odno={odno}")
                    continue

                tranche = session.get(Tranche, order.tranche_id)
                if not tranche:
                    continue

                ticker_obj = session.get(Ticker, tranche.ticker_id) if tranche.ticker_id else None
                if not ticker_obj:
                    continue

                cycle_no = tranche.cycle_number or 1
                session.add(Trade(
                    tranche_id=tranche.id, ticker=ticker_obj.ticker,
                    tranche_num=tranche.tranche_num, cycle_number=cycle_no,
                    side=side, order_type=order_type,
                    price=price, qty=qty, amount=amount, trade_date=trade_date,
                ))
                order.status = "filled"

                if side == "buy":
                    tranche.status = TrancheStatus.BOUGHT.value
                    tranche.avg_price = price
                    tranche.qty = qty
                    tranche.buy_price = price
                    tranche.buy_date = trade_date
                    tranche.days_held = 0
                    session.add(AppLog(level="INFO", message=
                        f"[{ticker_obj.ticker}] T{tranche.tranche_num} 매수 체결 (WS): ${price:.2f} x {qty}주"))
                else:
                    buy_amt = (tranche.avg_price or 0) * (tranche.qty or 0)
                    profit = round(amount - buy_amt, 2)
                    tranche.status = TrancheStatus.IDLE.value
                    tranche.avg_price = 0.0
                    tranche.qty = 0
                    tranche.buy_price = 0.0
                    tranche.buy_date = ""
                    tranche.days_held = 0
                    session.add(AppLog(level="INFO", message=
                        f"[{ticker_obj.ticker}] T{tranche.tranche_num} 매도 체결 (WS): ${price:.2f} x {qty}주 (손익 ${profit:.2f})"))

                session.commit()
                logger.info(f"[WS] 체결 기록: {ticker_obj.ticker} T{tranche.tranche_num} {side} {qty}주 @ ${price:.2f}")

        except Exception as e:
            logger.exception(f"[WS] 체결 처리 오류: {e}")


async def _run_ws_ccnl(cfg: dict):
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
        "header": {"approval_key": approval_key, "tr_type": "1", "custtype": "P"},
        "body": {"input": {"tr_id": tr_id, "tr_key": htsid}},
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
                tid, data_str = parts[1], parts[3]
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
                    out = (r.get("body") or {}).get("output", {})
                    if out:
                        data_map[tid] = {
                            "columns": data_map.get(tid, {}).get("columns", _CCNL_COLUMNS),
                            "encrypt": out.get("encrypt"), "key": out.get("key"), "iv": out.get("iv"),
                        }
                if h.get("tr_id") == "PINGPONG":
                    return raw
        except Exception as e:
            logger.debug(f"[WS] 메시지 처리: {e}")
        return None

    retries = 0
    while retries < 10:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                await ws.send(json.dumps(msg))
                logger.info(f"[WS] 체결통보 구독: HTS ID={htsid}")
                async for raw in ws:
                    if raw:
                        pong = await on_message(raw)
                        if pong:
                            await ws.send(pong)
        except asyncio.CancelledError:
            break
        except Exception as e:
            retries += 1
            logger.warning(f"[WS] 연결 끊김 (재시도 {retries}/10): {e}")
            await asyncio.sleep(min(30, retries * 5))


def start_ws_ccnl_thread():
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
