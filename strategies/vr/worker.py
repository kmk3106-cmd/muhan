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
    return out


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
