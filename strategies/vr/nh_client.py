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

# ── 응답 파싱 원칙 (2026-09 전면 수정) ─────────────────────────────────────────
# 블록·칸 이름은 **NH 명세(openapi.json)에 적힌 이름을 정확히** 쓴다. 칸 이름을 부분일치로
# 추측하던 코드가 시가를 종가로 읽고(8/22), 체결 목록 대신 요약 블록을 읽어 체결을 한 번도
# 반영하지 못하는(9/19 발견) 사고를 냈다. 블록은 API마다 다르다:
#   거래내역 Output_0=목록/Output_1=요약 · 잔고 Output_1=보유종목 · 일봉 Output_1=일봉
#   예약조회 Output_0=목록 · 예약제출 Output_0=접수번호


def _rows(r: dict, block: str) -> list[dict]:
    """응답에서 지정한 블록을 목록으로. 블록은 데이터가 있을 때만 내려온다(NH 명세)."""
    rows = r.get(block) or []
    if isinstance(rows, dict):
        rows = [rows]
    return rows


def norm_ticker(code) -> str:
    """NH 종목코드 정규화 — 거래내역은 'TQQQ US'(시장 접미사), 잔고는 'TQQQ' 로 온다."""
    return str(code or "").strip().split(" ")[0].upper()


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
    """일별 거래내역 — 체결 동기화용. 원본 행을 (거래일자, 일련번호) 오래된 순으로.

    [2026-09 수정] 예전 구현은 체결을 한 건도 읽지 못했다 (7월 실체결로 확인):
    - 목록은 **Output_0** 인데 Output_1(입금총액·세금합계 요약)을 먼저 읽었다
    - 종목 필터를 API 에 'TQQQ' 로 넘기면 **항상 0건** — NH 는 'TQQQ US' 형식.
      → 필터 없이 받아 여기서 거른다 (같은 계좌에 AMDY·DHA 등 타 종목이 있어 반드시 걸러야 함)
    - 한 응답이 20건에서 잘리고 rsp_cd 00218 — 봉투에 커서가 없어 기간을 반씩 쪼개 재조회
    """
    from datetime import datetime, timedelta
    want = norm_ticker(ticker) if ticker else ""
    out: list[dict] = []
    seen: set = set()

    def fetch(s: str, e: str, depth: int = 0) -> None:
        # 기간을 쪼개 재조회하면 호출이 몰려 NH 호출 한도(IGW42902, rate_limit)에 걸릴 수 있다.
        # 한도 초과면 잠시 쉬고 재시도 — 한 번 실패로 동기화 전체가 날아가지 않게.
        for attempt in range(4):
            try:
                r = call("/gbstock/inquiry/v1/dailyTransaction", {
                    "act_no": act_no, "iqr_sta_dt": s, "iqr_end_dt": e,
                    "act_trd_cfc_cd": "00", "iem_mlf_cd": "00001", "iem_cd": "",
                })
                break
            except NhplugError as ex:
                if getattr(ex, "category", "") != "rate_limit" or attempt == 3:
                    raise
                time.sleep(2.0 * (attempt + 1))
        truncated = str(r.get("rsp_cd")) == "00218"
        if truncated and s < e and depth < 12:
            ds, de = datetime.strptime(s, "%Y%m%d"), datetime.strptime(e, "%Y%m%d")
            mid = (ds + (de - ds) / 2).strftime("%Y%m%d")
            nxt = (datetime.strptime(mid, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
            fetch(s, mid, depth + 1)
            time.sleep(1.0)
            fetch(nxt, e, depth + 1)
            return
        if truncated:
            logger.warning("[VR] 거래내역 %s 하루치가 20건을 넘어 일부 누락 가능 (계좌 %s)", s, act_no[-4:])
        for x in _rows(r, "Output_0"):
            k = (str(x.get("trd_dt")), str(x.get("trd_sno")))
            if k not in seen:
                seen.add(k)
                out.append(x)

    fetch(start_dt, end_dt)
    if want:
        out = [x for x in out if norm_ticker(x.get("iem_cd")) == want]
    out.sort(key=lambda x: (str(x.get("trd_dt")), int(x.get("trd_sno") or 0)))
    return out


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
        rows = _rows(r, "Output_0")
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
    """최근 일봉 (날짜, 종가) 리스트 — 최신순.

    일봉은 **Output_1** (Output_0 은 현재가 요약). 칸: bsop_date 영업일 · close_prc 종가.
    (칸 이름 부분일치로 찾다가 open_prc(시가)를 종가로 읽은 적 있음 — 2026-08-22)
    """
    import datetime as _dt
    r = call("/gbstock/quote/v1/period", {
        "iem_cd": ticker, "end_dt": _dt.datetime.now().strftime("%Y%m%d"),
        "count": str(count), "maxavg": "0", "gubun": "3", "xtick": "0001",
        "today_cls": "1", "market_cls": "1",
    })
    out: list[tuple[str, float]] = []
    for row in _rows(r, "Output_1"):
        d = str(row.get("bsop_date") or row.get("trade_date") or "").strip()[:8]
        try:
            c = float(str(row.get("close_prc") or 0).replace(",", "").strip())
        except Exception:
            continue
        if len(d) == 8 and d.isdigit() and c > 0:
            out.append((d, c))
    if not out:
        logger.warning("[VR] %s 일봉 없음 (rsp %s) — 종가 산출 불가", ticker, r.get("rsp_cd"))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def last_close(ticker: str) -> tuple[str, float] | None:
    rows = daily_closes(ticker, 10)
    return rows[0] if rows else None


def submit_batch(act_no: str, ticker: str, rows: list[dict], start_dt: str, end_dt: str,
                 pause_sec: float = 0.35) -> list[dict]:
    """사다리 일괄 예약. rows: [{side, price, qty_acct}] → 결과에 성공/실패·접수번호 부착.

    시작일 자동 보정: 첫 건이 `23073`(시작일자 ≠ 예약주문일자)로 거부되면 시작일을
    다음 개장일로 밀어 한 번 더 시도한다. 달력에 없는 임시 휴장·조기폐장까지
    US_MARKET_HOLIDAYS 로 다 잡을 수는 없어서 두는 2차 방어선이다.
    (2026-09-05 노동절 사고 재발 방지 — 그때는 22건 전량 거부되고 조용히 끝났다)
    """
    from datetime import datetime, timedelta
    from .vr_logic import next_trading_day

    def _bump(cur: str) -> str:
        return next_trading_day(datetime.strptime(cur, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")

    results = []
    for r in rows:
        item = {"side": r["side"], "price": r["price"], "qty_acct": r["qty_acct"]}
        # 23073 이면 시작일만 밀어 같은 건을 재시도한다. 보정된 start_dt 는 이후 건에도 이어져
        # 한 배치가 서로 다른 시작일로 쪼개지지 않는다. (중복 제출 방지: 성공 즉시 루프 탈출)
        for attempt in range(6):
            try:
                resp = reserved_submit(act_no, ticker, r["side"], r["price"], r["qty_acct"],
                                       start_dt, end_dt)
                o0 = (_rows(resp, "Output_0") or [{}])[0]
                # 응답엔 예약접수번호(bkg_rtn_orr_no) 한 칸만 온다(명세). 예약주문일자(bkg_orr_dt)는
                # NH 규칙상 기간 시작일과 같다(23073) — 예약 취소에 필요해 시작일로 기록한다.
                no = str(o0.get("bkg_rtn_orr_no") or "")
                if not no:
                    logger.warning("[VR] 예약 접수번호 없음 — 응답: %s", o0)
                item.update({"ok": True, "nh_order_no": no, "nh_order_dt": start_dt, "raw": o0,
                             "start_dt": start_dt})
                break
            except NhplugError as e:
                if getattr(e, "code", "") == "23073" and attempt < 5:
                    nxt = _bump(start_dt)
                    logger.warning("[VR] 예약 시작일 %s 거부(23073) → %s 로 보정 재시도",
                                   start_dt, nxt)
                    start_dt = nxt
                    time.sleep(pause_sec)
                    continue
                item.update({"ok": False, "error": f"{e.category}/{getattr(e, 'code', '')}: {e}",
                             "start_dt": start_dt})
                break
            except Exception as e:  # pragma: no cover
                item.update({"ok": False, "error": str(e), "start_dt": start_dt})
                break
        results.append(item)
        time.sleep(pause_sec)  # 유량(429) 예방

    nfail = sum(1 for x in results if not x.get("ok"))
    if nfail:
        logger.warning("[VR] 예약 제출 %d/%d 실패 (계좌 %s, 기간 %s~%s) — 첫 사유: %s",
                       nfail, len(results), act_no[-4:], start_dt, end_dt,
                       next((x.get("error") for x in results if not x.get("ok")), ""))
    return results
