# -*- coding: utf-8 -*-
"""NH PLUG 해외주식(gbstock) 래퍼 — 공식 nhplug SDK(벤더링) 위임.

정본 스펙: https://www.nhplug.com/openapi-docs/gbstock/openapi.json (2026-08 확인).
예약주문 = 지정가(00) + 기간(bkg_orr_sta_dt~end_dt) + 잔량주문(bkg_orr_tp_cd=2)
→ 라오어 VR '2주간 기간잔량 지정가 예약매수/매도'와 1:1 대응.
"""
from __future__ import annotations

import logging
import time

from .config import load_nh_env

logger = logging.getLogger("trading_suite.vr.nh")

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
    """예약주문 조회 (상태: 접수/취소/전송/확인/거부/완료).

    한 응답이 15건에서 잘리고 rsp_cd=00218(연속조회 안내)이 뜬다. NH 봉투에 커서
    필드가 없어 연속조회 키를 알 수 없으므로, **매도(1)·매수(2)를 나눠 조회**해
    합친다. VR 사다리는 한쪽이 15건을 넘지 않아 잘림 없이 전량 조회된다.
    (2026-08-22 확인: 전체조회 15건 잘림 → 분리조회 매도 14 + 매수 8 = 22건 전량)
    """
    def _q(sby: str) -> list[dict]:
        r = call("/gbstock/inquiry/v1/reservedInquiry", {
            "act_no": act_no, "fc_mkt_dit_cd": NAT_US, "bkg_orr_dt": bkg_orr_dt,
            "iem_cd": ticker, "sby_dit_cd": sby, "bkg_orr_can_yn": "0",
            "oss_orr_knd_cd": "0", "bkg_orr_tp_cd": "0", "wtm_cur_knd_cd": "0",
        })
        rows = r.get("Output_1") or r.get("Output_0") or []
        if isinstance(rows, dict):
            rows = [rows]
        if str(r.get("rsp_cd")) == "00218":
            logger.warning("[VR] 예약조회 %s쪽이 15건에서 잘렸을 수 있음(00218)",
                           "매도" if sby == "1" else "매수")
        return rows

    out = _q("1") + _q("2")          # 매도 + 매수
    seen, uniq = set(), []
    for r in out:                    # 접수번호 기준 중복 제거
        k = (r.get("bkg_orr_dt"), r.get("bkg_rtn_orr_no"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def buyable(act_no: str, ticker: str, price: float) -> dict:
    """매수가능금액 조회 — NH 가 주문 수납 시 실제로 보는 숫자.

    잔고의 예수금(fc_dca)과 달리 미결제·담보·재사용까지 반영된 `orr_pbl_amt`(주문가능금액)이
    나온다. 주문 거부 여부는 이 값이 결정하므로 화면 '가용현금'은 이 값을 우선 쓴다.
    ⚠️ `fc_orr_uit_pr` 는 문자열이 아니라 **number(double)** — 문자열로 보내면 IGW40011.
    """
    r = call("/gbstock/inquiry/v1/buyableAmount", {
        "act_no": act_no, "pcs_dit": "1",            # 1.매수가능금액조회
        "fc_sec_trd_nat_cd": NAT_US, "iem_cd": ticker,
        "fc_orr_uit_pr": float(price),
        "wtm_cur_knd_cd": "1",                       # 거래국가통화
        "oss_orr_knd_cd": "1",                       # GTS(미국시장주문)
        "ahi_nmn_pr_tp_cd": "00",                    # 지정가
        "cfd_lon_cd": "00",                          # 현금
    })
    o = r.get("Output_0") or {}
    if isinstance(o, list):
        o = o[0] if o else {}
    return o


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

    # 종가 필드는 '우선순위 지정' 방식으로만 찾는다.
    # (NH 응답 키 순서가 open_prc, high, low, close_prc 라서 'prc' 같은 부분일치로
    #  훑으면 시가를 종가로 잘못 집는다. 2026-08-22 실제 오류.)
    CLOSE_KEYS = ("close_prc", "clos_prc", "close", "clpr", "stck_clpr",
                  "end_pr", "clsprc", "cls_prc")
    DATE_KEYS = ("bsop_date", "trade_date", "trad_date", "stck_bsop_date", "bass_dt", "date")
    BAD = ("open", "high", "low", "ostr", "hgst", "lwst")

    def _pick(row: dict, keys) -> str | None:
        low = {k.lower(): k for k in row}
        for want in keys:
            if want in low:
                return low[want]
        return None

    out: list[tuple[str, float]] = []
    for row in rows:
        dk = _pick(row, DATE_KEYS)
        ck = _pick(row, CLOSE_KEYS)
        if ck is None:                              # 알려진 키가 없을 때만 완화 탐색
            for k in row:
                lk = k.lower()
                if any(b in lk for b in BAD):
                    continue
                if any(t in lk for t in ("cls", "clpr", "close", "end_pr", "now_pr")):
                    ck = k
                    break
        if not dk or not ck:
            continue
        d = str(row[dk]).strip()[:8]
        if len(d) < 8 or not d.isdigit():
            continue
        try:
            c = float(str(row[ck]).replace(",", "").strip())
        except Exception:
            continue
        if c > 0:
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
