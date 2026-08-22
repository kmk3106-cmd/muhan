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
from .worker import sync_all, sync_gisu, apply_rollover, kill_switch_on

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
                    "cash": _cash_check(g, snaps.get(g["id"]))})
    return {"gisu": out, "kill_switch": kill_switch_on()}


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
    M.update_gisu(gid, **fields)
    return {"updated": len(fields), "fields": fields,
            "note": "가격 산출은 모델 수치 기준이라 배수 변경은 다음 미리보기/제출 수량부터 반영됩니다."}


@app.get("/api/gisu/{gid}/preview")
def preview(gid: str, e: float | None = None, start: str = "", end: str = ""):
    """다음 주기 자동 산출. e(마감 평가금) 미지정 시 모델잔여×최근종가."""
    g = M.get_gisu(gid)
    if not g:
        raise HTTPException(404, "기수 없음")
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
    prop = build_next_cycle({
        "v": g["v"], "pool_now": g["pool_now"], "g": g["g"],
        "model_qty": g["model_qty"], "unit": g["unit"],
        "buy_limit_pct": g["buy_limit_pct"], "sell_steps": g["sell_steps"],
        "mult": g["mult"], "cashflow": g["cashflow"],
        "week_no": g["week_no"], "cyc_end": g["cyc_end"],
    }, e_value=float(e))
    if start and end:
        prop["cyc_start"], prop["cyc_end"] = start, end
    prop["close_info"] = close_info
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
    for x in results:
        M.add_reserved(gid, body.week_no, x["side"], x["price"], x["qty_acct"],
                       body.cyc_start, body.cyc_end,
                       x.get("nh_order_dt", ""), x.get("nh_order_no", ""),
                       "submitted" if x.get("ok") else "failed",
                       x.get("raw") if x.get("ok") else {"error": x.get("error")})
    rolled = False
    if ok and not fail:
        apply_rollover(gid, {
            "week_no": body.week_no, "cyc_start": body.cyc_start, "cyc_end": body.cyc_end,
            "v": body.v, "band_lo": body.band_lo, "band_hi": body.band_hi,
            "pool_start": body.pool_start, "e_used": body.e_used,
        })
        rolled = True
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
