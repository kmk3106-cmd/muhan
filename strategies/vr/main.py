# -*- coding: utf-8 -*-
"""VR(밸류리밸런싱) FastAPI 서브앱 — NH PLUG 계좌 2개 (VR 0기 / VR 5기).

역할: 상태 조회 · 다음 주기 미리보기(자동 산출) · 예약 일괄 제출(사용자 버튼) ·
체결 동기화 · 라오어식 주차 그래프 데이터. 제출 외 자동 매매 없음.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import KILL_SWITCH_FILE
from . import models as M
from . import vr_logic as L
from .vr_logic import build_next_cycle, next_cycle_dates, r2
from .worker import (sync_all, sync_gisu, apply_rollover, kill_switch_on,
                     pending_target, pending_ready, effective_mult, finalize_mult_if_ready)

logger = logging.getLogger("vr")


@asynccontextmanager
async def lifespan(app: FastAPI):
    M.init_db()
    sched = None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler(timezone="Asia/Seoul")
        # 체결 동기화: 하루 2회 (미장 마감 후 10:00, 개장 전 22:00) — 읽기전용
        sched.add_job(sync_all, "cron", hour="10,22", minute=5, id="vr_sync", max_instances=1)
        # 토요일 자동 제출: auto_submit=ON 기수만 (금요일 종가 확정 후)
        from .worker import auto_submit_all
        sched.add_job(auto_submit_all, "cron", day_of_week="sat", hour=10, minute=30,
                      id="vr_auto_submit", max_instances=1, coalesce=True)
        sched.start()
        logger.info("[VR] 서브앱 기동 (sync 10:05/22:05 · 자동제출 토 10:30 KST)")
    except Exception as e:
        logger.warning(f"[VR] 스케줄러 미기동: {e}")
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="VR (NH)", lifespan=lifespan)


@app.get("/")
def root():
    return {"ok": True, "module": "vr"}


@app.get("/api/status")
def status():
    # 스냅샷이 30분 이상 오래됐으면 지연 갱신 (실패해도 무시 — 캐시 유지)
    try:
        import datetime as _dt
        snaps = {s["gisu_id"]: s for s in M.snapshots()}
        for g in M.all_gisu():
            s = snaps.get(g["id"])
            stale = True
            if s and s.get("updated_at"):
                try:
                    ts = _dt.datetime.strptime(s["updated_at"], "%Y-%m-%d %H:%M:%S")
                    stale = (_dt.datetime.now() - ts).total_seconds() > 1800
                except Exception:
                    pass
            if stale:
                from .worker import refresh_snapshot
                refresh_snapshot(g["id"])
    except Exception:
        pass
    snaps = {s["gisu_id"]: s for s in M.snapshots()}
    out = []
    for g in M.all_gisu():
        pend = [r for r in M.reserved_rows(g["id"], g["week_no"]) if r["status"] == "submitted"]
        out.append({**g, "kill_switch": kill_switch_on(),
                    "reserved_this_week": len(pend),
                    "snapshot": snaps.get(g["id"]),
                    "alert": _submit_alert(g["id"], g),
                    "qty_audit": _qty_audit(g, snaps.get(g["id"])),
                    "cash": _cash_check(g, snaps.get(g["id"])),
                    "pending": _pending_info(g, snaps.get(g["id"]))})
    return {"gisu": out, "kill_switch": kill_switch_on()}


def _pending_info(g: dict, snap: dict | None) -> dict | None:
    """배수 증액 매집 진행 상황 (매집 중 아니면 None)."""
    target = pending_target(g)
    if target <= 0:
        return None
    accum = int(g.get("pending_accum") or 0)
    close = float((snap or {}).get("close") or 0)
    left = max(0, target - accum)
    left_cost = r2(left * close) if close else None
    # 새 배수 기준 Pool 과 남은 입금 추정 (남은 매수대금은 지금 Pool 현금에서 나가므로 함께 더한다)
    pool_after = r2(float(g["pool_now"]) * int(g["pending_mult"]))
    pool_actual = float(_cash_check(g, snap)["pool_actual"])
    return {"from": int(g["mult"]), "to": int(g["pending_mult"]), "target": target,
            "accum": accum, "left": left, "left_cost": left_cost,
            "ready": pending_ready(g), "since": g.get("pending_since"),
            "next_submit": _next_submit_day(g),
            "pool_required_after": pool_after, "pool_actual": r2(pool_actual),
            "deposit_left": r2(max(0.0, pool_after - pool_actual + (left_cost or 0)))}


def _next_submit_day(g: dict) -> str:
    """다음 예약 제출일 = 이번 주기 종료일(금) 다음 날(토)."""
    import datetime as _dt
    try:
        return (_dt.datetime.strptime(str(g["cyc_end"]), "%Y%m%d") + _dt.timedelta(days=1)).strftime("%Y%m%d")
    except Exception:
        return ""


def _qty_audit(g: dict, snap: dict | None) -> dict:
    """체결 누락 감시 — NH 실보유와 모델의 '차이'가 기준에서 변했는지.

    모델잔여는 시스템이 반영한 체결로만 움직이고, NH 실보유는 실제 체결로 움직인다.
    체결을 빠짐없이 반영하고 있다면 둘의 차이(qty_offset — 넘겨받을 때부터 있던 차이·수동매매분)는
    변하지 않는다. 변했다 = 반영 못 한 체결이 있거나(동기화 고장) 앱에서 직접 매매했다는 뜻.
    (2026-09: 체결 조회가 한 번도 작동하지 않았는데 '체결 0건'이 정상처럼 보여 몰랐다)
    """
    s = snap or {}
    acct = int(s.get("qty") or 0)
    model = int(g["model_qty"]) * int(g["mult"])
    # 배수 증액 매집분은 모델 밖에서 일부러 모으는 주식이라 차이 계산에서 뺀다
    accum = int(g.get("pending_accum") or 0) if pending_target(g) > 0 else 0
    now = acct - model - accum
    base = g.get("qty_offset")
    out = {"acct_qty": acct, "model_qty_acct": model, "offset_now": now,
           "offset_base": base, "snap_at": s.get("updated_at"), "pending_accum": accum}
    if not s.get("updated_at"):
        out["state"] = "no_snapshot"
    elif base is None:
        out["state"] = "unset"
    elif now == int(base):
        out["state"] = "ok"
    else:
        out.update({"state": "mismatch", "diff": now - int(base)})
    return out


def _submit_alert(gid: str, g: dict) -> dict | None:
    """미해결 예약 실패 경고 — 자동제출이 조용히 실패해도 화면에서 바로 보이게.

    판정: 실패 기록이 있는 주차(현재 주차 이상)에 대해 **기대 사다리 건수 − 실제 접수 건수**.
    누적 실패 횟수를 세지 않으므로 같은 건을 여러 번 재시도해도 부풀지 않고,
    나중에 채워지면 자동으로 0이 되어 경고가 사라진다.
    (가격 매칭을 쓰지 않는 이유: 재시도 때 종가가 바뀌어 사다리 가격이 달라진다)

    2026-09 사고 대응: 9/5 5기 22건 전량 거부·8/29 0기 매수 8건 거부가 로그에만 남아
    일주일 넘게 아무도 몰랐다. 화면 경고가 없으면 같은 일이 반복된다.
    """
    try:
        rows = M.reserved_rows(gid)
        cur = int(g["week_no"])
        # 기대 사다리 건수 — 현 상태 기준(매수는 한도%로 정해지고, 매도는 설정 단수)
        try:
            exp_buy = len(L.buy_ladder(float(g["band_lo"]), int(g["model_qty"]),
                                       int(g["unit"]), float(g["pool_now"]),
                                       float(g["buy_limit_pct"])))
        except Exception:
            exp_buy = 0
        exp = {"buy": exp_buy, "sell": int(g["sell_steps"] or 0)}

        weeks = sorted({int(r["week_no"]) for r in rows
                        if int(r["week_no"]) >= cur and r["status"] == "failed"})
        unresolved, detail = 0, []
        for wk in weeks:
            wr = [r for r in rows if int(r["week_no"]) == wk]
            for side, label in (("buy", "매수"), ("sell", "매도")):
                s = sum(1 for r in wr if r["side"] == side and r["status"] == "submitted")
                miss = max(0, exp[side] - s)
                if miss:
                    unresolved += miss
                    detail.append(f"{wk}주차 {label} {miss}건")
        if unresolved <= 0:
            return None
        import json as _json
        last = [r for r in rows if r["status"] == "failed"]
        reason = ""
        if last:
            try:
                reason = str(_json.loads(last[-1].get("raw") or "{}").get("error", ""))
            except Exception:
                reason = ""
        # NH 원문에서 사람이 읽을 부분만 (앞의 category/code 접두어 제거)
        if "] " in reason:
            reason = reason.split("] ", 1)[1]
        return {"unresolved": unresolved, "detail": " · ".join(detail),
                "reason": reason[:160], "at": last[-1].get("created_at") if last else ""}
    except Exception as e:
        logger.warning(f"[VR:{gid}] 경고 판정 실패: {e}")
        return None


def _cash_check(g: dict, snap: dict | None) -> dict:
    """**모델 Pool(×배수) vs 실제 보유 Pool** 과부족.

    Pool 은 '주식이 아닌 나머지 자산' 전부다 — 현금만이 아니라 RP·원화자산·타종목까지
    포함한다. 그래서 비교 대상은 예수금이 아니라 **비(非)TQQQ 자산 총합**이다.

        실제 Pool = 예수금(달러환산) + 기타자산(수동입력: RP·원화·타종목 등)
        모델 Pool = pool_now × 배수
        과부족    = 실제 Pool − 모델 Pool

    ⚠️ NH PLUG 는 gbstock(해외주식) 전용 API 18개뿐이라 **RP·원화자산·국내자산이
    조회되지 않는다**(검증: 잔고 보유종목 TQQQ 1건, 예수금 0, 통화별 증거금 VND만).
    따라서 시스템이 자동으로 볼 수 있는 건 TQQQ 평가금과 외화예수금까지이고,
    나머지는 기수 설정의 `ext_assets` 에 수동 입력해 합산한다.

    ⚠️ NH 잔고는 같은 금액을 **원화/외화 두 벌로** 준다(krw_dca ↔ fc_dca,
    eal_amt_sum ↔ fc_eal_amt …). krw_dca 는 별도 원화 예수금이 아니라 예수금의
    원화 표시라 **더하면 두 번 센다** (검증: 68,416,749 ÷ 1393 = 49,114.68 = fc_dca).
    (단 매입금 abk_amt ↔ fc_abk_amt 는 매수 당시 환율이라 현재 환율과 다르다.
     그래서 환율 역산은 매입금이 아닌 **평가금** 쌍을 우선 쓴다 — worker 참조)
    """
    s = snap or {}
    fx = float(s.get("fx") or 0)
    usd = float(s.get("cash_usd") or 0)
    krw = float(s.get("cash_krw") or 0)
    order_amt = float(s.get("cash_order") or 0)
    krw_in_usd = round(krw / fx, 2) if (fx > 0 and krw) else 0.0
    same_pot = abs(krw_in_usd - usd) < 1.0   # 두 벌 표기가 일치 = 같은 지갑
    # 기타자산 = API 로 안 보이는 Pool 구성분 (수동). 달러분·원화분을 따로 받아 합산.
    ext_usd = float(g.get("ext_assets") or 0)
    ext_krw = float(g.get("ext_assets_krw") or 0)
    ext_krw_usd = round(ext_krw / fx, 2) if (fx > 0 and ext_krw) else 0.0
    ext = round(ext_usd + ext_krw_usd, 2)

    pool_actual = round(usd + ext, 2)
    pool_req = round(float(g["pool_now"]) * int(g["mult"]), 2)
    diff = round(pool_actual - pool_req, 2)

    # 보조 지표: 이번 주기 매수 사다리가 실제로 끌어쓸 금액 (한도% 적용분)
    try:
        buys = L.buy_ladder(float(g["band_lo"]), int(g["model_qty"]), int(g["unit"]),
                            float(g["pool_now"]), float(g["buy_limit_pct"]))
        need = round(sum(r["price"] * r["qty_model"] for r in buys) * int(g["mult"]), 2)
    except Exception:
        buys, need = [], 0.0

    eval_usd = float(s.get("eval_usd") or 0)
    return {
        "cash_usd": round(usd, 2), "cash_krw": round(krw, 2),
        "krw_in_usd": krw_in_usd, "same_pot": same_pot, "fx": fx,
        "ext_assets": round(ext, 2),
        "ext_usd": round(ext_usd, 2), "ext_krw": round(ext_krw, 2),
        "ext_krw_usd": ext_krw_usd,
        "order_amt": round(order_amt, 2),        # NH 주문가능금액 (참고)
        "pool_actual": pool_actual,              # 실제 보유 Pool (비TQQQ 자산)
        "pool_model": float(g["pool_now"]),
        "pool_required": pool_req,               # 모델 Pool × 배수
        "diff": diff, "short": diff < 0,
        "eval_usd": round(eval_usd, 2),
        "assets_usd": round(eval_usd + pool_actual, 2),   # 총자산 (전부 달러환산)
        "need_usd": need, "need_steps": len(buys),
        "api_blind": True,   # RP·원화·타종목은 API 로 안 보임 → 화면에 명시
        "updated_at": s.get("updated_at"),
    }


@app.get("/api/gisu/{gid}")
def gisu_detail(gid: str):
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
    return {
        "gisu": g,
        "weekly": M.weekly_rows(gid),
        "reserved": M.reserved_rows(gid)[-40:],
        "fills": M.fills_rows(gid, g["week_no"]),
    }


class SettingsBody(BaseModel):
    mult: int | None = None
    cashflow: float | None = None
    sell_steps: int | None = None
    ext_assets: float | None = None       # 기타자산 USD (RP·타종목 등, 수동)
    ext_assets_krw: float | None = None   # 기타자산 원화 (원화RP·예수금 등, 환율 자동환산)
    qty_offset: int | None = None         # 체결 누락 감시 기준 (NH 실보유 − 모델×배수) 재설정
    g: float | None = None
    buy_limit_pct: float | None = None
    auto_submit: int | None = None   # 1=토요일 자동 산출·제출


@app.patch("/api/gisu/{gid}/settings")
def gisu_settings(gid: str, body: SettingsBody):
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        return {"updated": 0}
    if "mult" in fields and fields["mult"] < 1:
        raise HTTPException(400, "배수는 1 이상")
    if "mult" in fields and int(fields["mult"]) != int(g["mult"]) and pending_target(g) > 0:
        raise HTTPException(409, "배수 증액 매집 중입니다 — [증액 취소] 후 변경하세요")
    M.update_gisu(gid, **fields)
    return {"updated": len(fields), "fields": fields,
            "note": "가격 산출은 모델 수치 기준이라 배수 변경은 다음 미리보기/제출 수량부터 반영됩니다."}


class MultPlanBody(BaseModel):
    to: int


def _mult_plan(g: dict, to: int) -> dict:
    """배수 증액 안내 수치 — 더 사야 할 주식, 입금 필요액, 적용 시점 (조회만, 저장 없음)."""
    snap = next((s for s in M.snapshots() if s["gisu_id"] == g["id"]), None) or {}
    cash = _cash_check(g, snap)
    m, mq, pool = int(g["mult"]), int(g["model_qty"]), float(g["pool_now"])
    d = int(to) - m
    close = float(snap.get("close") or 0)
    add_qty = d * mq
    add_cost = r2(add_qty * close) if close else None
    pool_req_after = r2(pool * int(to))
    # 입금 필요액: 주식 매수대금 + 새 배수 기준 Pool 부족분 (매수대금은 지금 Pool 현금에서 나가므로 함께 더한다)
    deposit = r2(max(0.0, pool_req_after - float(cash["pool_actual"]) + (add_cost or 0)))
    unit, cf = int(g["unit"]), float(g["cashflow"] or 0)
    buys = [float(r["price"]) for r in M.reserved_rows(g["id"], int(g["week_no"]))
            if r["side"] == "buy" and r["status"] == "submitted"]
    return {
        "gid": g["id"], "name": g["name"], "ticker": g["ticker"],
        "from": m, "to": int(to), "delta": d, "model_qty": mq,
        "add_qty": add_qty, "close": close or None, "add_cost": add_cost,
        "pool_model": r2(pool), "pool_add": r2(pool * d),
        "pool_required_now": r2(pool * m), "pool_required_after": pool_req_after,
        "pool_actual": cash["pool_actual"], "pool_short_now": r2(min(0.0, float(cash["diff"]))),
        "deposit_est": deposit,
        "step_qty_from": unit * m, "step_qty_to": unit * int(to),
        "cashflow_from": r2(cf * m), "cashflow_to": r2(cf * int(to)),
        "week_no": int(g["week_no"]), "cyc_end": g["cyc_end"],
        "next_submit": _next_submit_day(g), "auto_submit": int(g.get("auto_submit") or 0),
        "top_buy_limit": max(buys) if buys else None,
    }


@app.get("/api/gisu/{gid}/mult_plan")
def mult_plan(gid: str, to: int):
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
    if int(to) <= int(g["mult"]):
        raise HTTPException(400, f"지금 배수(×{g['mult']})보다 큰 값을 넣으세요 — 증액만 지원합니다")
    if int(to) > int(g["mult"]) + 50:
        raise HTTPException(400, "배수 값을 확인하세요")
    return _mult_plan(g, int(to))


@app.post("/api/gisu/{gid}/mult_plan")
def mult_plan_start(gid: str, body: MultPlanBody):
    """배수 증액 매집 시작 — 이후 NH 앱에서 산 주식은 매집분으로 잡혀 모델에서 빠진다."""
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
    to = int(body.to)
    if to <= int(g["mult"]) or to > int(g["mult"]) + 50:
        raise HTTPException(400, f"지금 배수(×{g['mult']})보다 큰 값을 넣으세요")
    if pending_target(g) > 0 and int(g["pending_mult"]) != to:
        raise HTTPException(409, f"이미 ×{g['pending_mult']} 증액 매집 중입니다 — [증액 취소] 후 다시 하세요")
    if pending_target(g) <= 0:
        import datetime as _dt
        M.update_gisu(gid, pending_mult=to, pending_accum=0,
                      pending_since=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info(f"[VR:{gid}] 배수 증액 매집 시작 ×{g['mult']} → ×{to} (목표 {(to - int(g['mult'])) * int(g['model_qty'])}주)")
    return {"started": True, **_mult_plan(M.get_gisu(gid), to)}


@app.delete("/api/gisu/{gid}/mult_plan")
def mult_plan_cancel(gid: str):
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
    if pending_target(g) <= 0:
        raise HTTPException(400, "진행 중인 배수 증액이 없습니다")
    accum = int(g.get("pending_accum") or 0)
    M.update_gisu(gid, pending_mult=None, pending_accum=0, pending_since=None)
    logger.info(f"[VR:{gid}] 배수 증액 취소 (×{g['pending_mult']}, 매집 {accum}주는 계좌에 남음)")
    return {"cancelled": True, "accum": accum,
            "note": (f"이미 산 {accum}주는 계좌에 그대로 남습니다 — 수량 감시 배너에서 기준 재설정하거나 매도하세요"
                     if accum else "")}


@app.get("/api/gisu/{gid}/preview")
def preview(gid: str, e: float | None = None, start: str = "", end: str = ""):
    """다음 주기 자동 산출. e(마감 평가금) 미지정 시 모델잔여×최근종가."""
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
    # 산출 직전에 이번 주기 체결을 모델에 반영 — 마지막 날(금) 체결이 정기 동기화(10:05) 전이면
    # 모델잔여·Pool 이 한 칸 늦은 채로 사다리가 나간다. 자동제출은 이미 먼저 동기화한다.
    try:
        sync_gisu(gid)
        g = M.get_gisu(gid)
    except Exception as ex:
        logger.warning(f"[VR:{gid}] 미리보기 전 체결 동기화 실패(기존 모델로 산출): {ex}")
    close_info = None
    if e is None:
        try:
            from . import nh_client as nh
            lc = nh.last_close(g["ticker"])
            if not lc:
                raise RuntimeError("종가 조회 실패")
            close_info = {"date": lc[0], "close": lc[1]}
            e = r2(int(g["model_qty"]) * lc[1])
        except Exception as ex:
            raise HTTPException(502, f"E 자동산출 실패({ex}) — e 파라미터로 직접 지정하세요")
    use_mult = effective_mult(g)     # 배수 증액 매집이 끝났으면 새 배수로 산출
    prop = build_next_cycle({
        "v": g["v"], "pool_now": g["pool_now"], "g": g["g"],
        "model_qty": g["model_qty"], "unit": g["unit"],
        "buy_limit_pct": g["buy_limit_pct"], "sell_steps": g["sell_steps"],
        "mult": use_mult, "cashflow": g["cashflow"],
        "week_no": g["week_no"], "cyc_end": g["cyc_end"],
    }, e_value=float(e))
    if start and end:
        prop["cyc_start"], prop["cyc_end"] = start, end
    prop["close_info"] = close_info
    if use_mult != int(g["mult"]):
        prop["mult_switch"] = {"from": int(g["mult"]), "to": use_mult}
    prop["current"] = {"week_no": g["week_no"], "cyc_end": g["cyc_end"],
                       "model_qty": g["model_qty"], "pool_now": g["pool_now"], "v": g["v"]}
    return prop


class SubmitBody(BaseModel):
    week_no: int
    cyc_start: str
    cyc_end: str
    e_used: float
    v: float
    band_lo: float
    band_hi: float
    pool_start: float
    rows: list[dict]   # [{side, price, qty_acct}]
    mult: int | None = None   # 미리보기에 쓴 배수 — 배수 증액 전환 확정 판정용


@app.post("/api/gisu/{gid}/submit")
def submit(gid: str, body: SubmitBody):
    """미리보기 확인 후 예약 일괄 제출 → 성공 시 주기 전환."""
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
    if kill_switch_on():
        raise HTTPException(423, "Kill Switch ON — 제출 차단")
    if body.week_no != int(g["week_no"]) + 2:
        raise HTTPException(409, f"주차 불일치: 현재 {g['week_no']} → 제출 {body.week_no} (기대 {int(g['week_no'])+2})")
    if not body.rows:
        raise HTTPException(400, "제출할 사다리 행 없음")
    from . import nh_client as nh
    results = nh.submit_batch(g["acct_no"], g["ticker"], body.rows,
                              body.cyc_start, body.cyc_end)
    ok = [x for x in results if x.get("ok")]
    fail = [x for x in results if not x.get("ok")]
    # 휴장일이면 submit_batch 가 시작일을 다음 개장일로 밀 수 있다 → 실제 사용된 값으로 기록
    eff_start = next((x.get("start_dt") for x in results if x.get("ok") and x.get("start_dt")),
                     body.cyc_start)
    for x in results:
        M.add_reserved(gid, body.week_no, x["side"], x["price"], x["qty_acct"],
                       eff_start, body.cyc_end,
                       x.get("nh_order_dt", ""), x.get("nh_order_no", ""),
                       "submitted" if x.get("ok") else "failed",
                       x.get("raw") if x.get("ok") else {"error": x.get("error")})
    rolled = False
    if ok and not fail:
        apply_rollover(gid, {
            "week_no": body.week_no, "cyc_start": eff_start, "cyc_end": body.cyc_end,
            "v": body.v, "band_lo": body.band_lo, "band_hi": body.band_hi,
            "pool_start": body.pool_start, "e_used": body.e_used,
        })
        rolled = True
        if body.mult:
            finalize_mult_if_ready(gid, int(body.mult))
    return {"submitted": len(ok), "failed": len(fail), "results": results,
            "rolled_over": rolled,
            "note": None if rolled else "일부 실패 — 주기 전환 보류. 실패건 확인 후 재시도/취소하세요."}


@app.post("/api/gisu/{gid}/sync")
def sync_now(gid: str):
    return sync_gisu(gid)


@app.get("/api/gisu/{gid}/graph")
def graph(gid: str):
    """라오어식 그래프 데이터: 주차별 평가금(실선)·최소/최대(점선)."""
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
    rows = M.weekly_rows(gid)
    live_eval = None
    try:
        from . import nh_client as nh
        lc = nh.last_close(g["ticker"])
        if lc:
            live_eval = r2(int(g["model_qty"]) * lc[1])
    except Exception:
        pass
    return {"weekly": rows, "current_week": g["week_no"], "live_eval": live_eval}


@app.get("/api/gisu/{gid}/reserved_live")
def reserved_live(gid: str):
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
    from . import nh_client as nh
    try:
        return {"rows": nh.reserved_inquiry(g["acct_no"], g["ticker"])[:60]}
    except Exception as e:
        raise HTTPException(502, f"예약 조회 실패: {e}")


@app.get("/api/kill_switch")
def ks_status():
    return {"active": kill_switch_on()}


@app.post("/api/kill_switch")
def ks_toggle(activate: bool):
    if activate:
        KILL_SWITCH_FILE.touch()
    else:
        KILL_SWITCH_FILE.unlink(missing_ok=True)
    return {"active": kill_switch_on()}
