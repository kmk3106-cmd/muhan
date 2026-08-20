# -*- coding: utf-8 -*-
"""NH PLUG 해외주식(gbstock) 래퍼 — 공식 nhplug SDK(벤더링) 위임.

정본 스펙: https://www.nhplug.com/openapi-docs/gbstock/openapi.json (2026-08 확인).
예약주문 = 지정가(00) + 기간(bkg_orr_sta_dt~end_dt) + 잔량주문(bkg_orr_tp_cd=2)
→ 라오어 VR '2주간 기간잔량 지정가 예약매수/매도'와 1:1 대응.
"""
from __future__ import annotations

import time

from .config import load_nh_env

load_nh_env()  # NHPLUG_APP_KEY 등 환경 주입 (SDK import 전에)

from .nhplug import call, NhplugError, get_token  # noqa: E402

NAT_US = "200"


def auth_ok() -> bool:
    try:
        return bool(get_token())
    except Exception:
        return False


def balance(act_no: str) -> dict:
    """미국주식 잔고: Output_0(요약) + Output_1(보유종목)."""
    return call("/gbstock/inquiry/v1/balance", {
        "act_no": act_no, "qut_iqr_dit_cd": "9",
        "fc_sec_trd_nat_cd": NAT_US, "cur_cd": "USD", "xns_dit_cd": "1",
    })


def daily_transactions(act_no: str, start_dt: str, end_dt: str, ticker: str = "") -> list[dict]:
    """일별 거래내역 (05.매수/06.매도 포함 전체) — 체결 동기화용."""
    r = call("/gbstock/inquiry/v1/dailyTransaction", {
        "act_no": act_no, "iqr_sta_dt": start_dt, "iqr_end_dt": end_dt,
        "act_trd_cfc_cd": "00", "iem_mlf_cd": "00001", "iem_cd": ticker,
    })
    rows = r.get("Output_1") or r.get("Output_0") or []
    if isinstance(rows, dict):
        rows = [rows]
    return rows


def reserved_submit(act_no: str, ticker: str, side: str, price: float, qty: int,
                    start_dt: str, end_dt: str) -> dict:
    """기간잔량 지정가 예약주문 제출. side: 'buy'|'sell'."""
    return call("/gbstock/order/v1/reservedSubmit", {
        "act_no": act_no,
        "fc_sec_trd_nat_cd": NAT_US,
        "iem_cd": ticker,
        "oss_sby_dit_cd": "2" if side == "buy" else "1",   # 1.매도 2.매수
        "orr_qty": int(qty),
        "fc_orr_uit_pr": float(price),
        "nmn_pr_tp_cd": "00",          # 지정가
        "oss_orr_knd_cd": "1",         # GTS(미국시장주문)
        "ose_ivs_sgy_cd": "0",         # 일반
        "bkg_orr_tp_cd": "2",          # 잔량주문 (기간 중 체결까지 유지)
        "bkg_orr_sta_dt": start_dt,
        "bkg_orr_end_dt": end_dt,
        "wtm_cur_knd_cd": "1",         # 거래국가통화(USD)
        "orr_pdt_dit_cd": "00",
        "cfd_lon_cd": "00",            # 현금
    })


def reserved_inquiry(act_no: str, ticker: str = "", bkg_orr_dt: str = "") -> list[dict]:
    """예약주문 조회 (상태: 접수/취소/전송/확인/거부/완료)."""
    r = call("/gbstock/inquiry/v1/reservedInquiry", {
        "act_no": act_no, "fc_mkt_dit_cd": NAT_US, "bkg_orr_dt": bkg_orr_dt,
        "iem_cd": ticker, "sby_dit_cd": "0", "bkg_orr_can_yn": "0",
        "oss_orr_knd_cd": "0", "bkg_orr_tp_cd": "0", "wtm_cur_knd_cd": "0",
    })
    rows = r.get("Output_1") or r.get("Output_0") or []
    if isinstance(rows, dict):
        rows = [rows]
    return rows


def reserved_cancel(act_no: str, ticker: str, bkg_orr_dt: str, bkg_rtn_orr_no: int) -> dict:
    return call("/gbstock/order/v1/reservedCancel", {
        "act_no": act_no, "fc_mkt_dit_cd": NAT_US, "bkg_orr_dt": bkg_orr_dt,
        "bkg_rtn_orr_no": int(bkg_rtn_orr_no), "iem_cd": ticker, "orr_pdt_dit_cd": "00",
    })


def daily_closes(ticker: str, count: int = 10) -> list[tuple[str, float]]:
    """최근 일봉 (날짜, 종가) 리스트 — 최신순. 필드명 이형에 견고하게 파싱."""
    import datetime as _dt
    r = call("/gbstock/quote/v1/period", {
        "iem_cd": ticker, "end_dt": _dt.datetime.now().strftime("%Y%m%d"),
        "count": str(count), "maxavg": "0", "gubun": "3", "xtick": "0001",
        "today_cls": "1", "market_cls": "1",
    })
    rows = r.get("Output_1") or r.get("Output_0") or []
    if isinstance(rows, dict):
        rows = [rows]
    out: list[tuple[str, float]] = []
    for row in rows:
        d = c = None
        for k, v in row.items():
            lk = k.lower()
            sv = str(v).strip()
            if d is None and ("dt" in lk or "date" in lk or "ymd" in lk) and len(sv) >= 8 and sv[:8].isdigit():
                d = sv[:8]
            if c is None and any(t in lk for t in ("cls", "clpr", "close", "end_pr", "now_pr", "prc")):
                try:
                    fv = float(sv.replace(",", ""))
                    if fv > 0:
                        c = fv
                except Exception:
                    pass
        if d and c:
            out.append((d, c))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def last_close(ticker: str) -> tuple[str, float] | None:
    rows = daily_closes(ticker, 10)
    return rows[0] if rows else None


def submit_batch(act_no: str, ticker: str, rows: list[dict], start_dt: str, end_dt: str,
                 pause_sec: float = 0.35) -> list[dict]:
    """사다리 일괄 예약. rows: [{side, price, qty_acct}] → 결과에 성공/실패·접수번호 부착."""
    results = []
    for r in rows:
        item = {"side": r["side"], "price": r["price"], "qty_acct": r["qty_acct"]}
        try:
            resp = reserved_submit(act_no, ticker, r["side"], r["price"], r["qty_acct"],
                                   start_dt, end_dt)
            o0 = resp.get("Output_0") or {}
            if isinstance(o0, list):
                o0 = o0[0] if o0 else {}
            no = ""
            dt = ""
            for k, v in (o0.items() if isinstance(o0, dict) else []):
                lk = k.lower()
                if not no and ("orr_no" in lk or "ord_no" in lk):
                    no = str(v)
                if not dt and ("dt" in lk and str(v).strip()[:8].isdigit()):
                    dt = str(v).strip()[:8]
            item.update({"ok": True, "nh_order_no": no, "nh_order_dt": dt, "raw": o0})
        except NhplugError as e:
            item.update({"ok": False, "error": f"{e.category}/{getattr(e, 'code', '')}: {e}"})
        except Exception as e:  # pragma: no cover
            item.update({"ok": False, "error": str(e)})
        results.append(item)
        time.sleep(pause_sec)  # 유량(429) 예방
    return results
