# -*- coding: utf-8 -*-
"""VR 워커 — 체결 동기화(모델 반영) + 주기 전환 적용.

- sync_gisu: NH 일별거래내역에서 현 주기 TQQQ 체결을 가져와 모델 잔여/Pool 갱신
  (계좌수량 ÷ 배수 = 모델수량. 사다리 가격 체결이므로 모델 정합 유지)
- apply_rollover: 예약 일괄 제출 성공 후 상태를 다음 주기로 전환
- 주문 '생성'은 없다 — 제출은 사용자가 UI에서 미리보기 확인 후 버튼으로만.
"""
from __future__ import annotations

import logging
from datetime import datetime

from .config import KILL_SWITCH_FILE
from .vr_logic import r2
from . import models as M

logger = logging.getLogger("vr")


def kill_switch_on() -> bool:
    return KILL_SWITCH_FILE.exists()


def _parse_fill(row: dict, ticker: str = "") -> dict | None:
    """NH 일별거래내역 1행 → {side, qty, price, dt, sno}. 대상 종목의 매수·매도 외(입금·배당·타종목)는 None.

    칸은 NH 명세 이름 그대로 쓴다 (2026-09 수정 — 예전엔 칸 이름을 추측해 찾았다):
      act_trd_tp_nm 계좌거래유형명(매수/매도/입금…) · iem_cd 종목코드('TQQQ US')
      trd_qty 거래수량 · trd_uit_pr 거래단가 · trd_dt 거래일자 · trd_sno 거래일련번호
    중복판정은 호출부에서 **거래일자-일련번호**(NH 가 거래마다 부여, 불변)로 한다.
    예전엔 행 전체 해시라 NH 가 한 칸(거래후잔고·환율 등)만 바꿔 다시 줘도 같은 체결을 두 번 반영했다.
    """
    from .nh_client import norm_ticker
    kind = str(row.get("act_trd_tp_nm") or "").strip()
    side = "buy" if kind == "매수" else ("sell" if kind == "매도" else None)
    if side is None:
        return None
    if ticker and norm_ticker(row.get("iem_cd")) != norm_ticker(ticker):
        return None
    try:
        qty = int(round(float(str(row.get("trd_qty") or 0).replace(",", ""))))
        price = float(str(row.get("trd_uit_pr") or 0).replace(",", ""))
    except Exception:
        return None
    dt = str(row.get("trd_dt") or "").strip()[:8]
    sno = str(row.get("trd_sno") or "").strip()
    if qty <= 0 or price <= 0 or len(dt) != 8 or not sno:
        logger.warning(f"[VR] 체결행 형식 이상 — 건너뜀: {row}")
        return None
    return {"side": side, "qty": qty, "price": price, "dt": dt, "sno": sno}


def refresh_snapshot(gid: str) -> dict | None:
    """NH 잔고+최근종가 → 계좌 스냅샷 캐시 (대시보드 총자산 합산용, 읽기전용 API)."""
    g = M.get_gisu(gid)
    if not g:
        return None
    from . import nh_client as nh
    try:
        bal = nh.balance(g["acct_no"])
        qty = 0
        buy_usd = 0.0
        # 잔고 보유종목은 Output_1. 수량 = cns_bse_bnc_qty(체결기준잔고수량), 매입금 = fc_abk_amt (명세 이름)
        for row in nh._rows(bal, "Output_1"):
            if nh.norm_ticker(row.get("iem_cd")) != nh.norm_ticker(g["ticker"]):
                continue
            try:
                qty = int(float(str(row.get("cns_bse_bnc_qty") or 0).replace(",", "")))
                buy_usd = float(str(row.get("fc_abk_amt") or 0).replace(",", ""))
            except Exception as e:
                logger.warning(f"[VR:{gid}] 잔고 행 해석 실패: {e} — {row}")
            break
        # 계좌 현금 — 외화(fc_dca)·원화(krw_dca) 를 따로 담는다.
        # 환율은 별도 조회 없이 같은 응답에서 역산: 동일 자산의 원화평가 ÷ 외화평가.
        # (평가금 → 매입금 → 총자산 순으로 대체. 전부 0이면 fx=0 으로 두고 환산 생략)
        o0 = bal.get("Output_0") or {}
        if isinstance(o0, list):
            o0 = o0[0] if o0 else {}

        def _f(key: str) -> float:
            try:
                return float(str(o0.get(key, 0)).replace(",", "").strip() or 0)
            except Exception:
                return 0.0

        fx = 0.0
        for kw, usd in (("eal_amt_sum", "fc_eal_amt"), ("abk_amt", "fc_abk_amt"),
                        ("tot_aet_amt", "fc_aet_amt")):
            a, b = _f(kw), _f(usd)
            if a > 0 and b > 0:
                fx = round(a / b, 2)
                break
        cash_usd, cash_krw = _f("fc_dca"), _f("krw_dca")

        # 주문가능금액 — NH 가 주문 수납 시 실제로 보는 값(미결제·담보·재사용 반영).
        # 1단 매수가를 기준가로 넘긴다. 실패해도 예수금 폴백으로 계속 진행.
        cash_order = 0.0
        try:
            px = round(float(g["band_lo"]) / max(1, int(g["model_qty"])), 2)
            ob = nh.buyable(g["acct_no"], g["ticker"], px)
            cash_order = float(str(ob.get("orr_pbl_amt", 0) or 0).replace(",", ""))
        except Exception as e:
            logger.warning(f"[VR:{gid}] 주문가능금액 조회 실패(예수금으로 대체): {e}")

        lc = nh.last_close(g["ticker"])
        close = lc[1] if lc else 0.0
        eval_usd = r2(qty * close) if close > 0 else 0.0
        M.upsert_snapshot(gid, qty, r2(buy_usd), close, eval_usd,
                          r2(cash_usd), round(cash_krw, 2), fx, r2(cash_order))
        return {"qty": qty, "buy_usd": buy_usd, "close": close, "eval_usd": eval_usd,
                "cash_usd": cash_usd, "cash_krw": cash_krw, "fx": fx,
                "cash_order": cash_order}
    except Exception as e:
        logger.warning(f"[VR:{gid}] 스냅샷 갱신 실패: {e}")
        return None


# ─────────────────────────── 배수 증액 매집 ───────────────────────────
# 배수를 올리려면 계좌가 (새 배수 − 현 배수) × 모델잔여 만큼 주식을 더 가져야 한다.
# 사용자가 NH 앱에서 직접 사는 이 주식은 사다리 체결이 아니므로 VR 모델에 넣으면 안 된다.
# NH 체결내역에는 예약체결/수동매매를 가르는 칸이 없어서(실데이터 39칸 확인),
# 매집 중에는 **이번 주차 매수 예약의 가격·수량**으로 판정한다:
#   지정가 매수는 한도가 이하로만 체결되므로, 체결가 이상 한도가를 가진 예약의 남은 수량까지만
#   사다리 체결로 보고 나머지는 매집분으로 모델에서 뺀다. (매도는 판정 대상 아님 — 기존 그대로)
# 새 배수는 매집이 목표에 닿은 뒤 **다음 주기 예약부터** 적용한다(이번 주기 예약은 기존 수량).

def pending_target(g: dict) -> int:
    """매집 목표 수량 = (목표 배수 − 현 배수) × 모델잔여. 매집 중 아니면 0."""
    pm, m = int(g.get("pending_mult") or 0), int(g.get("mult") or 1)
    if pm <= m:
        return 0
    return (pm - m) * int(g.get("model_qty") or 0)


def pending_ready(g: dict) -> bool:
    t = pending_target(g)
    return t > 0 and int(g.get("pending_accum") or 0) >= t


def effective_mult(g: dict) -> int:
    """다음 주기 예약에 쓸 배수 — 매집이 목표에 닿았으면 목표 배수, 아니면 현 배수."""
    return int(g["pending_mult"]) if pending_ready(g) else max(1, int(g["mult"]))


def _buy_capacity(gid: str, g: dict) -> list[list[float]]:
    """이번 주차 매수 예약의 남은 수량 [[한도가, 남은수량], …] — 이미 반영한 사다리 매수분은 차감."""
    cap = [[float(r["price"]), int(r["qty_acct"])] for r in M.reserved_rows(gid, int(g["week_no"]))
           if r["side"] == "buy" and r["status"] == "submitted"]
    for f in M.fills_rows(gid, int(g["week_no"])):
        if f["side"] == "buy":
            _consume(cap, float(f["price"]), int(f["qty_acct"]) - int(f.get("ext_qty") or 0))
    return cap


def _consume(cap: list[list[float]], price: float, qty: int) -> int:
    """체결가 price 로 받을 수 있는 예약(한도가 ≥ 체결가) 수량을 차감하고 사다리분 수량을 반환.

    한도가가 가장 낮은 예약부터 쓴다 — 높은 한도 예약은 더 높은 가격 체결에도 쓸 수 있으니 남겨 둔다.
    """
    left = int(qty)
    for c in sorted((c for c in cap if c[0] + 0.005 >= price and c[1] > 0), key=lambda c: c[0]):
        take = min(left, int(c[1]))
        c[1] -= take
        left -= take
        if left <= 0:
            break
    return int(qty) - left


def finalize_mult_if_ready(gid: str, used_mult: int) -> bool:
    """예약 제출(주기 전환) 성공 후 호출 — 새 배수로 예약을 냈으면 배수 전환을 확정한다.

    매집분이 목표보다 많으면 그 차이는 모델 밖 주식이므로 체결 감시 기준(qty_offset)에 더한다.
    """
    g = M.get_gisu(gid)
    if not g or not g.get("pending_mult") or int(used_mult) != int(g["pending_mult"]):
        return False
    target, accum = pending_target(g), int(g.get("pending_accum") or 0)
    fields = {"mult": int(g["pending_mult"]), "pending_mult": None,
              "pending_accum": 0, "pending_since": None}
    if g.get("qty_offset") is not None:
        fields["qty_offset"] = int(g["qty_offset"]) + (accum - target)
    M.update_gisu(gid, **fields)
    logger.info(f"[VR:{gid}] 배수 전환 확정 ×{g['mult']} → ×{g['pending_mult']} "
                f"(매집 {accum}주 / 목표 {target}주)")
    return True


def sync_gisu(gid: str) -> dict:
    """현 주기 체결을 모델에 반영. 반환: {new_fills, model_qty, pool_now}."""
    g = M.get_gisu(gid)
    if not g:
        return {"error": "gisu 없음"}
    from . import nh_client as nh
    today = datetime.now().strftime("%Y%m%d")
    rows = nh.daily_transactions(g["acct_no"], g["cyc_start"], today, g["ticker"])
    new = 0
    mq, pool = int(g["model_qty"]), float(g["pool_now"])
    mult = max(1, int(g["mult"]))
    pending = pending_target(g) > 0
    cap = _buy_capacity(gid, g) if pending else None
    accum = int(g.get("pending_accum") or 0)
    for row in rows:
        f = _parse_fill(row, g["ticker"])
        if not f:
            continue
        # 중복판정 키 = 기수:거래일자-일련번호. dedup_key 는 전 기수 공통 UNIQUE 라 기수를 붙인다
        # (두 계좌에 같은 날 같은 일련번호가 나올 수 있음)
        key = f"{gid}:{f['dt']}-{f['sno']}"
        if M.fill_exists(key):
            continue  # 이미 반영
        ext = 0
        if pending and f["side"] == "buy":
            ext = f["qty"] - _consume(cap, f["price"], f["qty"])   # 예약으로 설명 안 되는 매수 = 매집분
        if not M.add_fill(gid, g["week_no"], f["side"], f["price"], f["qty"], f["dt"], key, ext):
            continue  # 이미 반영
        ladder_q = f["qty"] - ext
        if ladder_q > 0:
            model_q = max(1, round(ladder_q / mult))
            amt = r2(f["price"] * model_q)
            if f["side"] == "buy":
                mq += model_q
                pool = r2(pool - amt)
            else:
                mq -= model_q
                pool = r2(pool + amt)
        if ext:
            accum += ext
            logger.info(f"[VR:{gid}] 배수 증액 매집분 {ext}주 @{f['price']} ({f['dt']}) — 모델 제외, 누적 {accum}주")
        new += 1
    if new:
        upd = {"model_qty": mq, "pool_now": pool}
        if pending:
            upd["pending_accum"] = accum
        M.update_gisu(gid, **upd)
        logger.info(f"[VR:{gid}] 체결 {new}건 반영 → 모델잔여 {mq}, Pool {pool}"
                    + (f", 매집 {accum}/{pending_target({**g, 'model_qty': mq})}주" if pending else ""))
    return {"new_fills": new, "model_qty": mq, "pool_now": pool}


def sync_all() -> dict:
    if kill_switch_on():
        return {"skipped": "kill_switch"}
    out = {}
    for g in M.all_gisu():
        try:
            out[g["id"]] = sync_gisu(g["id"])
        except Exception as e:
            logger.warning(f"[VR:{g['id']}] sync 실패: {e}")
            out[g["id"]] = {"error": str(e)}
        try:
            refresh_snapshot(g["id"])
        except Exception:
            pass
    return out


def auto_submit_all() -> dict:
    """[토요일 자동] auto_submit=ON 기수 중 주기가 끝난 것을 산출→예약 제출→주기 전환.

    안전장치: Kill Switch / 주기 미종료 스킵 / E·사다리 무결성 확인 /
    전량 성공 시에만 주기 전환(부분 실패면 전환 보류 — 대시보드에서 수동 처리).
    """
    from datetime import datetime as _dt
    from .vr_logic import build_next_cycle
    from . import models as _M
    report: dict = {}
    if kill_switch_on():
        return {"skipped": "kill_switch"}
    today = _dt.now().strftime("%Y%m%d")
    for g in _M.all_gisu():
        gid = g["id"]
        if not int(g.get("auto_submit") or 0):
            report[gid] = "auto_submit OFF"
            continue
        if today <= str(g["cyc_end"]):
            report[gid] = f"주기 진행중(~{g['cyc_end']})"
            continue
        try:
            sync_gisu(gid)                      # 최신 체결 반영 후 산출
            g = _M.get_gisu(gid)
            from . import nh_client as nh
            lc = nh.last_close(g["ticker"])
            if not lc or lc[1] <= 0:
                report[gid] = "E 산출 실패(종가 조회) — 수동 제출 필요"
                continue
            e_val = r2(int(g["model_qty"]) * lc[1])
            use_mult = effective_mult(g)        # 배수 증액 매집이 끝났으면 새 배수
            prop = build_next_cycle({
                "v": g["v"], "pool_now": g["pool_now"], "g": g["g"],
                "model_qty": g["model_qty"], "unit": g["unit"],
                "buy_limit_pct": g["buy_limit_pct"], "sell_steps": g["sell_steps"],
                "mult": use_mult, "cashflow": g["cashflow"],
                "week_no": g["week_no"], "cyc_end": g["cyc_end"],
            }, e_value=e_val)
            rows = ([{"side": "buy", "price": r["price"], "qty_acct": r["qty_acct"]} for r in prop["buys"]]
                    + [{"side": "sell", "price": r["price"], "qty_acct": r["qty_acct"]} for r in prop["sells"]])
            if not rows:
                report[gid] = "사다리 0건 — 수동 확인 필요"
                continue
            results = nh.submit_batch(g["acct_no"], g["ticker"], rows,
                                      prop["cyc_start"], prop["cyc_end"])
            ok = [x for x in results if x.get("ok")]
            fail = [x for x in results if not x.get("ok")]
            # 휴장일 보정으로 시작일이 밀렸을 수 있다 → 실제 사용된 값으로 기록·전환
            eff_start = next((x.get("start_dt") for x in results if x.get("ok") and x.get("start_dt")),
                             prop["cyc_start"])
            prop = {**prop, "cyc_start": eff_start}
            for x in results:
                _M.add_reserved(gid, prop["week_no"], x["side"], x["price"], x["qty_acct"],
                                prop["cyc_start"], prop["cyc_end"],
                                x.get("nh_order_dt", ""), x.get("nh_order_no", ""),
                                "submitted" if x.get("ok") else "failed",
                                x.get("raw") if x.get("ok") else {"error": x.get("error")})
            if ok and not fail:
                apply_rollover(gid, {**prop, "e_used": e_val})
                switched = finalize_mult_if_ready(gid, use_mult)
                report[gid] = (f"자동 제출 완료: {prop['week_no']}주차 {len(ok)}건 (E=${e_val}, 종가 {lc[0]} ${lc[1]})"
                               + (f" · 배수 ×{use_mult} 전환" if switched else ""))
                logger.info(f"[VR:{gid}] {report[gid]}")
            else:
                report[gid] = f"부분 실패: 성공 {len(ok)} / 실패 {len(fail)} — 주기 전환 보류, 수동 확인"
                logger.warning(f"[VR:{gid}] {report[gid]}")
        except Exception as e:
            report[gid] = f"자동 제출 오류: {e}"
            logger.warning(f"[VR:{gid}] {report[gid]}")
    return report


def apply_rollover(gid: str, proposal: dict) -> None:
    """예약 제출 성공 후: 이전 주차 마감 기록 + 상태를 다음 주기로 전환.

    proposal: build_next_cycle 반환 형태 (+ e_used).
    """
    g = M.get_gisu(gid)
    if not g:
        raise ValueError("gisu 없음")
    # 이전 주차 마감 기록 (E, pool_end, 거래액 = pool_end − pool_start)
    traded = r2(float(g["pool_now"]) - float(g["pool_start"]))
    M.upsert_weekly(gid, int(g["week_no"]),
                    eval_amt=float(proposal["e_used"]),
                    pool_end=float(g["pool_now"]),
                    traded_amt=traded)
    # 새 주차 상태
    M.update_gisu(
        gid,
        week_no=int(proposal["week_no"]),
        cyc_start=proposal["cyc_start"], cyc_end=proposal["cyc_end"],
        v=float(proposal["v"]), band_lo=float(proposal["band_lo"]),
        band_hi=float(proposal["band_hi"]),
        pool_start=float(proposal["pool_start"]), pool_now=float(proposal["pool_start"]),
    )
    M.upsert_weekly(gid, int(proposal["week_no"]),
                    v=float(proposal["v"]), band_lo=float(proposal["band_lo"]),
                    band_hi=float(proposal["band_hi"]),
                    pool_start=float(proposal["pool_start"]))
    logger.info(f"[VR:{gid}] 주기 전환: {g['week_no']}→{proposal['week_no']}주차 "
                f"V={proposal['v']} 기간 {proposal['cyc_start']}~{proposal['cyc_end']}")
