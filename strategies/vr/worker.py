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


def _parse_fill(row: dict) -> dict | None:
    """dailyTransaction 행 → {side, qty, price, dt, key}. 매수/매도 외 행(입출금 등)은 None."""
    side = None
    qty = price = None
    dt = ""
    key_parts = []
    for k, v in row.items():
        lk = k.lower()
        sv = str(v).strip()
        key_parts.append(f"{k}={sv}")
        if side is None and ("cfc" in lk or "sby" in lk or "trd" in lk):
            if sv == "05" or "매수" in sv:
                side = "buy"
            elif sv == "06" or "매도" in sv:
                side = "sell"
        if qty is None and "qty" in lk:
            try:
                q = int(float(sv.replace(",", "")))
                if q > 0:
                    qty = q
            except Exception:
                pass
        if price is None and ("uit_pr" in lk or "prc" in lk or ("pr" in lk and "prd" not in lk)):
            try:
                p = float(sv.replace(",", ""))
                if p > 0:
                    price = p
            except Exception:
                pass
        if not dt and "dt" in lk and sv[:8].isdigit():
            dt = sv[:8]
    if side and qty and price:
        import hashlib
        key = hashlib.sha256("|".join(sorted(key_parts)).encode()).hexdigest()[:24]
        return {"side": side, "qty": qty, "price": price, "dt": dt, "key": key}
    return None


def refresh_snapshot(gid: str) -> dict | None:
    """NH 잔고+최근종가 → 계좌 스냅샷 캐시 (대시보드 총자산 합산용, 읽기전용 API)."""
    g = M.get_gisu(gid)
    if not g:
        return None
    from . import nh_client as nh
    try:
        bal = nh.balance(g["acct_no"])
        rows = bal.get("Output_1") or []
        if isinstance(rows, dict):
            rows = [rows]
        qty = 0
        buy_usd = 0.0
        for row in rows:
            if str(row.get("iem_cd", "")).strip().upper() != g["ticker"].upper():
                continue
            for k, v in row.items():
                lk = k.lower()
                sv = str(v).replace(",", "").strip()
                if "bnc_qty" in lk:
                    try:
                        qty = int(float(sv))
                    except Exception:
                        pass
                if lk == "fc_abk_amt":
                    try:
                        buy_usd = float(sv)
                    except Exception:
                        pass
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

        lc = nh.last_close(g["ticker"])
        close = lc[1] if lc else 0.0
        eval_usd = r2(qty * close) if close > 0 else 0.0
        M.upsert_snapshot(gid, qty, r2(buy_usd), close, eval_usd,
                          r2(cash_usd), round(cash_krw, 2), fx)
        return {"qty": qty, "buy_usd": buy_usd, "close": close, "eval_usd": eval_usd,
                "cash_usd": cash_usd, "cash_krw": cash_krw, "fx": fx}
    except Exception as e:
        logger.warning(f"[VR:{gid}] 스냅샷 갱신 실패: {e}")
        return None


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
    for row in rows:
        f = _parse_fill(row)
        if not f:
            continue
        if not M.add_fill(gid, g["week_no"], f["side"], f["price"], f["qty"], f["dt"], f["key"]):
            continue  # 이미 반영
        model_q = max(1, round(f["qty"] / mult))
        amt = r2(f["price"] * model_q)
        if f["side"] == "buy":
            mq += model_q
            pool = r2(pool - amt)
        else:
            mq -= model_q
            pool = r2(pool + amt)
        new += 1
    if new:
        M.update_gisu(gid, model_qty=mq, pool_now=pool)
        logger.info(f"[VR:{gid}] 체결 {new}건 반영 → 모델잔여 {mq}, Pool {pool}")
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
            prop = build_next_cycle({
                "v": g["v"], "pool_now": g["pool_now"], "g": g["g"],
                "model_qty": g["model_qty"], "unit": g["unit"],
                "buy_limit_pct": g["buy_limit_pct"], "sell_steps": g["sell_steps"],
                "mult": g["mult"], "cashflow": g["cashflow"],
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
            for x in results:
                _M.add_reserved(gid, prop["week_no"], x["side"], x["price"], x["qty_acct"],
                                prop["cyc_start"], prop["cyc_end"],
                                x.get("nh_order_dt", ""), x.get("nh_order_no", ""),
                                "submitted" if x.get("ok") else "failed",
                                x.get("raw") if x.get("ok") else {"error": x.get("error")})
            if ok and not fail:
                apply_rollover(gid, {**prop, "e_used": e_val})
                report[gid] = f"자동 제출 완료: {prop['week_no']}주차 {len(ok)}건 (E=${e_val}, 종가 {lc[0]} ${lc[1]})"
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
