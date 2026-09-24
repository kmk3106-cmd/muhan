# -*- coding: utf-8 -*-
"""trading_suite 부모 FastAPI - 두 전략 sub-app 마운트 + 통합 네이티브 UI.

단일 프로세스·단일 포트(8000)에서 무한매수법/떨사오팔을 함께 운용한다.
UI는 통합 SPA가 각 전략의 검증된 백엔드 API를 호출해 구성한다(iframe 미사용).
트레이딩 코어/strategies 는 무수정 — 부모는 마운트·집계·라우팅만.
"""
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from strategies.infinite.main import app as infinite_app
from strategies.ddsop.main import app as ddsop_app
from strategies.jongsa.main import app as jongsa_app
from strategies.vr.main import app as vr_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("trading_suite")

SUB_APPS = {"infinite": infinite_app, "ddsop": ddsop_app, "jongsa": jongsa_app}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        for name, sub in SUB_APPS.items():
            await stack.enter_async_context(sub.router.lifespan_context(sub))
            logger.info(f"[suite] sub-app lifespan 기동: {name}")
        # VR(NH 계좌)은 KIS 3전략 집계(STRATS/ADAPTERS)와 분리된 독립 모듈 — 별도 기동
        await stack.enter_async_context(vr_app.router.lifespan_context(vr_app))
        logger.info("[suite] sub-app lifespan 기동: vr (NH)")
        sched = None
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from core.equity_snapshot import snapshot as _equity_snapshot
            from core.t_audit import run as _t_audit_run
            sched = BackgroundScheduler(timezone="Asia/Seoul")
            sched.add_job(_equity_snapshot, "interval", minutes=30,
                          id="equity_snapshot", max_instances=1)
            sched.add_job(_equity_snapshot, "date",
                          run_date=datetime.now() + timedelta(seconds=12),
                          id="equity_snapshot_b1")
            sched.add_job(_equity_snapshot, "date",
                          run_date=datetime.now() + timedelta(seconds=75),
                          id="equity_snapshot_b2")
            # 무한매수법 T값 일일 감시 — 매일 09:00 KST + 기동 직후 1회
            sched.add_job(_t_audit_run, "cron", hour=9, minute=0,
                          id="t_audit_daily", max_instances=1, coalesce=True)
            sched.add_job(_t_audit_run, "date",
                          run_date=datetime.now() + timedelta(seconds=45),
                          id="t_audit_bootstrap")
            # metrics 캐시: 기동 즉시 예열(동기 1회) + 15초 주기 갱신 → 첫 화면부터 즉시 응답
            try:
                _metrics_refresh()
                logger.info("[suite] metrics 캐시 예열 완료")
            except Exception as _e:
                logger.warning(f"[suite] metrics 예열 실패(계속 진행): {_e}")
            sched.add_job(_metrics_refresh, "interval", seconds=15,
                          id="metrics_refresh", max_instances=1, coalesce=True)
            # 토스 계좌 스냅샷(조회 전용): 10분 주기 + 기동 직후 1회. 대시보드는 캐시만 읽는다.
            try:
                from core.toss_client import refresh as _toss_refresh, configured as _toss_ok
                if _toss_ok():
                    sched.add_job(lambda: _toss_refresh(force=True), "interval", minutes=10,
                                  id="toss_snapshot", max_instances=1, coalesce=True)
                    sched.add_job(lambda: _toss_refresh(force=True), "date",
                                  run_date=datetime.now() + timedelta(seconds=25),
                                  id="toss_snapshot_boot")
                    logger.info("[suite] 토스 계좌 스냅샷 스케줄 시작 (10분 주기, 조회 전용)")
                else:
                    logger.info("[suite] 토스 자격 미설정 — 스냅샷 스케줄 생략")
            except Exception as _e:
                logger.warning(f"[suite] 토스 스냅샷 스케줄 실패: {_e}")
            sched.start()
            logger.info("[suite] equity 스냅샷터(30분) + T값 일일감사(09:00 KST) 스케줄 시작")
        except Exception as e:
            logger.warning(f"[suite] equity 스냅샷터 미시작: {e}")
        logger.info("[suite] 전체 기동 완료 (port 8000)")
        yield
        if sched:
            sched.shutdown(wait=False)
        logger.info("[suite] 종료 중...")


app = FastAPI(title="trading_suite (멀티전략)", lifespan=lifespan)
app.mount("/infinite", infinite_app)
app.mount("/ddsop", ddsop_app)
app.mount("/jongsa", jongsa_app)
app.mount("/vr", vr_app)


class BudgetBody(BaseModel):
    total_usd: float


@app.get("/api/suite/strategies")
def suite_strategies():
    from core.ticker_registry import all_active
    from core.strategy_budget import summary
    return {"active_tickers": all_active(), "budgets": summary()}


@app.get("/api/suite/t_audit")
def api_t_audit_latest():
    """무한매수법 T값 일일감사 — 최신 결과 1건."""
    from core.t_audit import latest
    return latest() or {"ts": "", "overall": "none", "items": []}


@app.get("/api/suite/t_audit/history")
def api_t_audit_history(limit: int = 30):
    """T값 감사 이력 (시간 오름차순)."""
    from core.t_audit import history
    return {"items": history(limit)}


@app.post("/api/suite/t_audit/run")
def api_t_audit_run_now():
    """T값 감사 즉시 1회 실행 (UI '지금 검증' 버튼)."""
    from core.t_audit import run
    return run()


@app.get("/api/suite/journal")
def api_journal(date: str = ""):
    """전략별 일자별 매매일지 (네이버 블로그 복붙용). date=YYYYMMDD, 없으면 최근 체결일."""
    from core.blog_journal import daily, latest_active_date
    latest = latest_active_date()
    d = (date or latest).replace("-", "")[:8]
    out = daily(d)
    out["latest_date"] = latest
    return out


@app.get("/api/suite/strategy_intros")
def api_strategy_intros():
    """전략별 소개(첫 글용) 텍스트 — 매수규칙·매도·손절·로직."""
    from core.blog_journal import strategy_intros
    return {"items": strategy_intros()}


# ---- metrics 캐시 (대시보드 첫 로딩 지연 제거) ----
# 워커가 KIS 잔고·체결(90일)을 조회하는 수십 초 동안 DB/GIL 경합으로 build_metrics 가
# 최대 30초까지 밀렸다. 캐시본을 즉시 돌려주고 갱신은 백그라운드에서 수행한다.
_METRICS: dict = {"data": None, "ts": 0.0, "building": False}
_METRICS_TTL = 20.0        # 이 시간 지나면 백그라운드 갱신 트리거
_METRICS_HARD = 300.0      # 이 시간 넘게 갱신 실패면 동기 계산(최초 기동 포함)


def _metrics_refresh() -> dict | None:
    import time as _t
    from core.suite_metrics import build_metrics
    if _METRICS["building"]:
        return None
    _METRICS["building"] = True
    try:
        d = build_metrics()
        # DB 락 등으로 일부 전략 조회가 실패하면(ok=False) 정상 캐시본을 덮지 않는다
        # — 화면에 원금·손익이 0/None 으로 튀는 것을 방지.
        bad = [s for s in d.get("strategies", []) if s.get("cycles_ok") is False]
        if bad and _METRICS["data"] is not None:
            logger.warning(f"[suite] metrics 일부 실패({[s['strategy'] for s in bad]}) — 이전 캐시 유지")
            return _METRICS["data"]
        _METRICS["data"], _METRICS["ts"] = d, _t.time()
        return d
    except Exception as e:
        logger.warning(f"[suite] metrics 갱신 실패: {e}")
        return None
    finally:
        _METRICS["building"] = False


@app.get("/api/suite/metrics")
def suite_metrics(fresh: bool = False):
    """캐시 우선 반환(즉시) + 오래됐으면 백그라운드 갱신. fresh=1이면 동기 재계산.

    캐시가 아직 없으면(기동 직후) 최대 3초만 기다리고, 그래도 없으면
    'warming' 응답을 즉시 돌려준다 — 워커 sync와 겹쳐도 화면이 멈추지 않도록.
    """
    import time as _t
    import threading
    age = _t.time() - _METRICS["ts"]
    if fresh:
        d = _metrics_refresh()
        if d is not None:
            return {**d, "cache": {"age_sec": 0, "stale": False}}
    if _METRICS["data"] is None:
        if not _METRICS["building"]:
            threading.Thread(target=_metrics_refresh, daemon=True).start()
        for _ in range(8):                       # 최대 0.8초만 대기 (워커와 겹쳐도 화면 안 멈춤)
            if _METRICS["data"] is not None:
                break
            _t.sleep(0.1)
        if _METRICS["data"] is None:             # 아직 준비 전 — 빈 골격 즉시 반환
            return {
                "generated_at": "", "warming": True,
                "account": {}, "combined": {}, "automation": {"active": 0, "total": 0, "running": False},
                "strategies": [], "recent_trades": [], "holdings": {"ts": "", "items": []},
                "nh": {"accounts": [], "strategies": [], "account": {}, "eval_total": 0},
                "cache": {"age_sec": None, "stale": True},
            }
    if (age > _METRICS_TTL or age > _METRICS_HARD) and not _METRICS["building"]:
        threading.Thread(target=_metrics_refresh, daemon=True).start()
    return {**_METRICS["data"], "cache": {"age_sec": round(age, 1), "stale": age > _METRICS_TTL}}


class CompoundBody(BaseModel):
    mode: str          # 'simple' | 'compound'


@app.get("/api/suite/compound")
def api_compound_status():
    """전략별 단리/복리 상태 + 목표 시드(증액분·현금상한 반영)."""
    from core.compound_mode import all_states, target_seed
    out = []
    for k, st in all_states().items():
        t = target_seed(k)
        out.append({"strategy": k, **st,
                    "current_seed": t["base_seed"] + st["added"],
                    "gain": t["gain"], "target_seed": t["capped_target"],
                    "raw_target": t["raw_target"], "cap_note": t["note"] or st.get("cap_note", "")})
    return {"items": out}


@app.post("/api/suite/compound/{strategy}")
def api_compound_set(strategy: str, body: CompoundBody):
    """단리/복리 전환. 단리 전환 시 시드를 '시드 할당 총액' 기준으로 복원."""
    from core.compound_mode import set_mode, restore_simple
    if strategy not in ("infinite", "ddsop", "jongsa"):
        raise HTTPException(400, "지원하지 않는 전략")
    if body.mode == "simple":
        st = set_mode(strategy, "simple")
        r = restore_simple(strategy)
        return {"state": st, "restore": r}
    st = set_mode(strategy, "compound")
    return {"state": st, "note": "다음 싸이클 종료 시부터 실현손익만큼 시드가 증액됩니다."}


@app.post("/api/suite/toss/refresh")
def api_toss_refresh():
    """토스 계좌 스냅샷 강제 갱신 — 잔고·예수금 조회만 한다(주문 기능 없음)."""
    from core.toss_client import refresh, configured
    if not configured():
        raise HTTPException(400, "토스 자격 미설정 (/root/trading_suite_state/toss.env)")
    d = refresh(force=True)
    return {"ok": bool(d.get("ok")), "ts": d.get("ts", ""), "eval_usd": d.get("eval_usd", 0),
            "cash_usd": d.get("cash_usd", 0), "items": len(d.get("items") or []),
            "error": d.get("error", "")}


@app.get("/api/suite/toss/series")
def api_toss_series():
    """토스 계좌 자산 추이 — 스냅샷마다 기록한 점들(평가+예수금). 조회만."""
    from core.toss_client import series
    return series()


@app.get("/api/suite/series")
def suite_series():
    from core.equity_snapshot import series
    return series()


class CashflowBody(BaseModel):
    date: str
    kind: str           # 'deposit' | 'withdraw'
    amount: float
    memo: str = ""


@app.get("/api/suite/cashflow")
def suite_cashflow_list():
    """입출금 내역 (사용자 기록)."""
    from core.cashflow_ledger import list_entries, summary
    return {"entries": list_entries(), "summary": summary()}


@app.post("/api/suite/cashflow")
def suite_cashflow_add(body: CashflowBody):
    from core.cashflow_ledger import add_entry
    try:
        rec = add_entry(body.date, body.kind, body.amount, body.memo)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return rec


@app.delete("/api/suite/cashflow/{entry_id}")
def suite_cashflow_del(entry_id: int):
    from core.cashflow_ledger import delete_entry
    if not delete_entry(entry_id):
        raise HTTPException(404, "해당 입출금 기록 없음")
    return {"deleted": entry_id}


@app.post("/api/suite/strategies/{name}/budget")
def set_strategy_budget(name: str, body: BudgetBody):
    from core.strategy_budget import set_assigned_total
    try:
        set_assigned_total(name, body.total_usd)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"strategy": name, "assigned_total": body.total_usd}


# 전략 표시 메타 (데이터 주도 · 신규 전략은 SUB_APPS + 여기 항목만 추가).
# kind: 전략관리 폼/엔드포인트 분기용 (infinite=Portfolio API, ddsop=Ticker API)
_STRAT_META = {
    "infinite": {"sub": "무한매수법 V2.2 · 40분할", "icon": "fa-infinity", "kind": "infinite",
        "logic": "라오어식 무한매수법 V2.2. 시드를 A회(기본 40)로 분할해 매일 LOC 분할매수 — "
                 "전반전(T<20)은 평단·☆% 2분할 공격 매수, 후반전/40회차 도달 시 쿼터손절"
                 "(QUARTER) 모드로 전환. 평단가 대비 +R% 도달 시 LOC 전량매도로 싸이클 종료."},
    "ddsop": {"sub": "떨사오팔 · n트렌치", "icon": "fa-droplet", "kind": "ddsop",
        "logic": "떨어지면 사고 오르면 판다. 총액을 n개 트렌치로 분할 — 전일 종가 −x% 에 "
                 "트렌치 1칸 LOC 매수, 평단가 +x% 에 LOC 매도. 보유 N거래일(손절일) 경과 "
                 "트렌치는 MOC 손절매도. 첫 트렌치 매도로 싸이클 종료."},
    "jongsa": {"sub": "종사종팔 · n트렌치", "icon": "fa-clock-rotate-left", "kind": "jongsa",
        "logic": "종가에 사고 종가에 판다. 총액을 n개 트렌치로 분할 — 매 거래일 다음 트렌치 1칸을 "
                 "종가 LOC로 매수(한도=전일종가+15%, 거의 무조건 종가체결 · 수량=트렌치금액/전일종가). "
                 "※ KIS가 MOC 매수를 불허(매도전용)해 LOC로 종가매수. 매도는 보유 전 트렌치의 "
                 "전체평단(가중평균) +목표%(기본 3.5%) 도달 시 전량 LOC 일괄매도(목표가 보장·부분매도 없음), "
                 "40거래일 경과 트렌치는 MOC 손절. 전량 매도로 싸이클 종료."},
    "infinite_v3": {"sub": "무한매수법 v3.0", "icon": "fa-infinity", "kind": "infinite",
        "logic": "무한매수법 v3.0 개선 로직.  ※ 추후 신규 개발 예정."},
}

_SHELL_HTML = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#ffffff">
<title>trading_suite · 자동매매 대시보드</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/css/all.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--blue:#2f6bff;--blue-s:#eaf0ff;--indigo:#6c5ce7;--green:#16a34a;--green-s:#e8f6ec;
--red:#e5484d;--red-s:#fde8e8;--amber:#d97706;--amber-s:#fdf2e3;--bg:#f3f5f9;--card:#fff;
--line:#e9edf3;--c0:#1a2233;--c1:#5b6577;--c2:#9aa3b2;--c3:#cdd4df;--sb:236px;--hd:66px;
--sh:0 1px 2px rgba(20,28,46,.04),0 2px 8px rgba(20,28,46,.05);--rd:13px}
*{box-sizing:border-box;margin:0;padding:0}html,body{height:100%}
body{font-family:'Noto Sans KR','본고딕','Malgun Gothic','맑은 고딕',
'Apple SD Gothic Neo',system-ui,sans-serif;
background:var(--bg);color:var(--c0);-webkit-font-smoothing:antialiased;
font-size:calc(15px*var(--ts-fs,1));letter-spacing:-.2px}
::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:var(--c3);
border-radius:8px;border:2px solid transparent;background-clip:content-box}
a{color:inherit;text-decoration:none}
.wrap{display:flex;min-height:100vh}
.sb{width:var(--sb);background:var(--card);border-right:1px solid var(--line);position:fixed;
inset:0 auto 0 0;display:flex;flex-direction:column;z-index:30}
.brand{display:flex;align-items:center;gap:10px;padding:17px 18px;border-bottom:1px solid var(--line)}
.brand .m{width:34px;height:34px;border-radius:9px;color:#fff;display:flex;align-items:center;
justify-content:center;font-size:calc(15px*var(--ts-fs,1));background:linear-gradient(135deg,var(--blue),var(--indigo))}
.brand b{font-size:calc(16px*var(--ts-fs,1));font-weight:800;letter-spacing:-.2px}
.brand small{display:block;font-size:calc(10px*var(--ts-fs,1));color:var(--c2);letter-spacing:.2em;margin-top:1px}
.nav{flex:1;overflow-y:auto;padding:10px}
.ni{display:flex;align-items:center;gap:12px;width:100%;text-align:left;border:none;
background:none;cursor:pointer;padding:12px 13px;border-radius:9px;color:var(--c1);
font-family:inherit;font-size:calc(14.5px*var(--ts-fs,1));font-weight:600;margin-bottom:3px;transition:.14s}
.ni:hover{background:var(--bg);color:var(--c0)}
.ni.on{background:var(--blue);color:#fff;font-weight:600;box-shadow:0 4px 12px rgba(47,107,255,.28)}
.ni .i{width:19px;text-align:center;font-size:calc(15px*var(--ts-fs,1));opacity:.7}.ni.on .i{opacity:1}
.ni .ch{margin-left:auto;font-size:calc(10px*var(--ts-fs,1));opacity:.4}
.sbhelp{margin:10px;padding:14px;border-radius:11px;background:var(--bg);font-size:calc(12.5px*var(--ts-fs,1));color:var(--c1)}
.sbhelp b{display:block;color:var(--c0);font-size:calc(13.5px*var(--ts-fs,1));margin-bottom:4px}
.mn{flex:1;margin-left:var(--sb);min-width:0;display:flex;flex-direction:column}
.hd{height:var(--hd);background:var(--card);border-bottom:1px solid var(--line);display:flex;
align-items:center;gap:16px;padding:0 24px;position:sticky;top:0;z-index:20}
.hamb{display:none;border:none;background:none;font-size:calc(18px*var(--ts-fs,1));color:var(--c1);cursor:pointer}
.hd .ttl{font-size:calc(19px*var(--ts-fs,1));font-weight:800;letter-spacing:-.3px}
.hd .sp{flex:1}
.hd .st{display:flex;align-items:center;gap:7px;font-size:calc(13px*var(--ts-fs,1));font-weight:600;color:var(--c1)}
.hd .st .d{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px var(--green-s)}
.hd .st.off .d{background:var(--red);box-shadow:0 0 0 3px var(--red-s)}
.hd .dt{font-size:calc(13px*var(--ts-fs,1));color:var(--c2);font-variant-numeric:tabular-nums}
.btn{font-family:inherit;padding:9px 15px;border-radius:9px;font-size:calc(13.5px*var(--ts-fs,1));font-weight:600;
cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--c1);
display:inline-flex;align-items:center;gap:7px;transition:.14s}
.btn:hover{background:var(--bg);color:var(--c0)}
.btn.p{background:var(--blue);color:#fff;border-color:var(--blue)}
.btn.p:hover{background:#2358e0;color:#fff}
.btn.sm{padding:6px 11px;font-size:calc(12.5px*var(--ts-fs,1))}
.btn.dg{color:var(--red);border-color:var(--red-s)}.btn.dg:hover{background:var(--red-s)}
.body{padding:24px 26px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(196px,1fr));gap:14px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--rd);padding:16px 18px;
box-shadow:var(--sh);display:flex;align-items:flex-start;gap:12px}
.kpi .ic{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;
justify-content:center;font-size:calc(14px*var(--ts-fs,1));background:var(--blue-s);color:var(--blue);flex-shrink:0}
.kpi .ic.g{background:var(--green-s);color:var(--green)}.kpi .ic.r{background:var(--red-s);color:var(--red)}
.kpi .ic.n{background:var(--bg);color:var(--c2)}.kpi .ic.a{background:var(--amber-s);color:var(--amber)}
.kpi .lab{font-size:calc(13px*var(--ts-fs,1));color:var(--c1);font-weight:500}
.kpi .v{font-size:calc(25px*var(--ts-fs,1));font-weight:800;color:var(--c0);margin-top:7px;letter-spacing:-.5px;
font-variant-numeric:tabular-nums;line-height:1.1}
.kpi .v.up{color:var(--green)}.kpi .v.down{color:var(--red)}
.kpi .v small{font-size:calc(13px*var(--ts-fs,1));font-weight:600;color:var(--c2)}
.kpi .s{font-size:calc(12px*var(--ts-fs,1));margin-top:7px;color:var(--c2);font-variant-numeric:tabular-nums}
.up{color:var(--green);font-weight:700}.dn{color:var(--red);font-weight:700}
.grid{display:grid;gap:16px;margin-top:16px}
.g-3-1{grid-template-columns:2fr 1fr}.g-2{grid-template-columns:1fr 1fr}
@media(max-width:1080px){.g-3-1,.g-2{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--rd);box-shadow:var(--sh)}
.ch{display:flex;align-items:center;gap:10px;padding:15px 18px;border-bottom:1px solid var(--line)}
.ch .ct{font-size:calc(15.5px*var(--ts-fs,1));font-weight:800;flex:1}.ch .ct i{color:var(--c3);margin-right:7px}
.ch .lk{font-size:calc(12.5px*var(--ts-fs,1));color:var(--blue);font-weight:600;cursor:pointer}
.seg{display:flex;gap:2px;background:var(--bg);padding:3px;border-radius:8px}
.sgb{border:none;background:none;color:var(--c2);font-family:inherit;font-weight:600;font-size:calc(12.5px*var(--ts-fs,1));
padding:6px 12px;border-radius:6px;cursor:pointer}.sgb.on{background:#fff;color:var(--c0);box-shadow:var(--sh)}
.cw{position:relative;height:264px;padding:14px 16px}
.donut-w{position:relative;height:200px}.donut-c{position:absolute;inset:0;display:flex;
flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
.donut-c s{font-size:calc(12px*var(--ts-fs,1));color:var(--c2)}.donut-c b{font-size:calc(20px*var(--ts-fs,1));font-weight:800;margin-top:2px}
.lg{padding:6px 18px 16px;display:flex;flex-direction:column;gap:8px}
.lg .r{display:flex;align-items:center;gap:8px;font-size:calc(13.5px*var(--ts-fs,1))}
.lg .r i{width:9px;height:9px;border-radius:3px;flex-shrink:0}.lg .r .n{flex:1;color:var(--c1)}
.lg .r .a{font-weight:700;font-variant-numeric:tabular-nums}.lg .r .p{color:var(--c2);width:46px;text-align:right}
.bars{padding:14px 18px;display:flex;flex-direction:column;gap:13px}
.bar{font-size:calc(13.5px*var(--ts-fs,1))}.bar .t{display:flex;justify-content:space-between;margin-bottom:5px}
.bar .t b{font-weight:700;font-variant-numeric:tabular-nums}
.bar .tr{height:8px;background:var(--bg);border-radius:6px;overflow:hidden}
.bar .tr i{display:block;height:100%;border-radius:6px}
.tbl{width:100%;border-collapse:collapse;font-size:calc(14px*var(--ts-fs,1))}
.tbl th{text-align:left;color:var(--c1);font-weight:600;padding:13px 18px;
border-bottom:1px solid var(--line);font-size:calc(12.5px*var(--ts-fs,1));letter-spacing:0;background:#f7f9fc}
.tbl td{padding:13px 18px;border-bottom:1px solid var(--line);color:var(--c1);font-variant-numeric:tabular-nums}
.tbl tbody tr:hover{background:#fafbfe}.tbl tr:last-child td{border-bottom:0}.tbl td b{color:var(--c0)}
.dn8{display:inline-flex;align-items:center;gap:8px}.dn8 i{width:8px;height:8px;border-radius:50%}
.bdg{padding:5px 11px;border-radius:16px;font-size:calc(12.5px*var(--ts-fs,1));font-weight:700;display:inline-flex;align-items:center;gap:5px}
.bdg::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.bdg.run{background:var(--green-s);color:#15803d}.bdg.stop{background:var(--red-s);color:#c23030}
.bdg.part{background:var(--amber-s);color:#b45309}
.tag{padding:4px 10px;border-radius:6px;font-size:calc(12.5px*var(--ts-fs,1));font-weight:700}
.tag.buy{background:var(--red-s);color:#c23030}.tag.sell{background:var(--blue-s);color:var(--blue)}
.al{display:flex;gap:10px;padding:13px 18px;border-bottom:1px solid var(--line);font-size:calc(13px*var(--ts-fs,1))}
.al:last-child{border-bottom:0}.al .ad{width:7px;height:7px;border-radius:50%;margin-top:5px;flex-shrink:0}
.al .ad.e{background:var(--red)}.al .ad.w{background:var(--amber)}.al .ad.i{background:var(--green)}
.al .am{flex:1;color:var(--c1);line-height:1.5;word-break:break-all}
.al .at{display:block;color:var(--c2);font-size:calc(10.5px*var(--ts-fs,1));margin-top:3px;font-variant-numeric:tabular-nums}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:200px;
color:var(--c2);text-align:center;gap:9px;padding:30px}.empty i{font-size:calc(28px*var(--ts-fs,1));color:var(--c3)}
.empty .t{font-size:calc(14.5px*var(--ts-fs,1));font-weight:700;color:var(--c1)}.empty .s{font-size:calc(12.5px*var(--ts-fs,1))}
.muted{color:var(--c2);font-size:calc(13.5px*var(--ts-fs,1));padding:26px;text-align:center}
.form{padding:18px;display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:720px){.form{grid-template-columns:1fr}}
.fld label{display:block;font-size:calc(13px*var(--ts-fs,1));color:var(--c1);font-weight:600;margin-bottom:7px}
.fld input,.fld select{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:9px;
font-family:inherit;font-size:calc(14.5px*var(--ts-fs,1));color:var(--c0);background:#fff}
.fld input:focus,.fld select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-s)}
.fnote{grid-column:1/-1;font-size:calc(12.5px*var(--ts-fs,1));color:var(--c2)}
.fact{grid-column:1/-1;display:flex;gap:9px;justify-content:flex-end}
.tip{font-size:calc(13px*var(--ts-fs,1));color:var(--c1);background:var(--blue-s);padding:12px 15px;border-radius:9px;
margin:0 18px 16px;display:flex;gap:8px;align-items:flex-start}.tip i{color:var(--blue);margin-top:2px}
/* 강조 음영박스 — 핵심 수치·주의문구용 */
.hl{background:var(--blue-s);border:1px solid #d6e2ff;border-radius:10px;padding:12px 15px}
.hl.g{background:var(--green-s);border-color:#c5e8d1}
.hl.r{background:var(--red-s);border-color:#f6ccce}
.hl.a{background:var(--amber-s);border-color:#f3ddb6}
.hl.n{background:var(--bg);border-color:var(--line)}
.hl b{font-weight:800}
.kpi.hi{background:linear-gradient(180deg,#f7faff,#fff);border-color:#d6e2ff;
box-shadow:0 2px 10px rgba(47,107,255,.10)}
.numbox{display:inline-block;background:var(--bg);border:1px solid var(--line);border-radius:8px;
padding:3px 9px;font-weight:700;font-variant-numeric:tabular-nums}
.numbox.g{background:var(--green-s);border-color:#c5e8d1;color:#15803d}
.numbox.r{background:var(--red-s);border-color:#f6ccce;color:#c23030}
.hidden{display:none!important}
.scrim{display:none;position:fixed;inset:0;background:rgba(20,28,46,.45);z-index:25}
.toast{position:fixed;right:20px;bottom:20px;background:var(--c0);color:#fff;padding:14px 19px;
border-radius:10px;font-size:calc(13.5px*var(--ts-fs,1));z-index:50;box-shadow:0 8px 24px rgba(0,0,0,.2);opacity:0;
transform:translateY(8px);transition:.2s;pointer-events:none}.toast.s{opacity:1;transform:none}
@media(max-width:980px){.sb{transform:translateX(-100%);transition:.25s}.sb.open{transform:none}
.mn{margin-left:0}.hamb{display:block}.scrim.show{display:block}.body{padding:14px}}
.vtblk{padding:15px 18px;border-bottom:1px solid var(--line)}.vtblk:last-child{border-bottom:0}
.vth{display:flex;align-items:center;gap:10px;font-size:calc(14.5px*var(--ts-fs,1))}
.trcells{display:flex;flex-wrap:wrap;gap:6px;padding:11px 0 10px}
.trc{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;
border-radius:8px;background:var(--bg);color:var(--c2);font-size:calc(12.5px*var(--ts-fs,1));font-weight:700;
border:1px solid var(--line)}
.trc.on{background:var(--green-s);color:#15803d;border-color:#bfe6cc}
.vtblk details summary{cursor:pointer;font-size:calc(11.5px*var(--ts-fs,1));color:var(--blue);font-weight:600;
list-style:none;display:inline-block}.vtblk details summary::-webkit-details-marker{display:none}
.vtblk details[open] summary{margin-bottom:9px}

/* ============ MOBILE OPTIMIZATION (Android + iPhone) ============ */
/* iOS 글자 자동확대 방지 · 가로 오버플로 차단 · 부드러운 스크롤 */
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{overflow-x:hidden}
/* iOS Safari 동적 툴바 대응: 100vh → 100dvh (지원 시) */
@supports(min-height:100dvh){.wrap{min-height:100dvh}}
/* 가로 스크롤 영역(테이블 등) 관성 스크롤 */
.tblw,.tbl-scroll,[data-scrollx]{-webkit-overflow-scrolling:touch}

/* iPhone 노치/홈 인디케이터 안전영역 — 사이드바·헤더·토스트·스크림 */
.sb{padding-left:env(safe-area-inset-left);
padding-bottom:env(safe-area-inset-bottom)}
.hd{padding-top:env(safe-area-inset-top);
padding-left:max(24px,env(safe-area-inset-left));
padding-right:max(24px,env(safe-area-inset-right));
height:calc(var(--hd) + env(safe-area-inset-top))}
.scrim{padding:0}

/* 태블릿/세로 — 980px 이하: 사이드바 드로어 + 안전영역 보정 */
@media(max-width:980px){
  .hd{gap:10px}
  .hd .ttl{font-size:calc(16.5px*var(--ts-fs,1));flex:0 1 auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .hamb{min-width:40px;min-height:40px;display:flex;align-items:center;justify-content:center;
  margin-left:calc(-1 * 6px)}
  .sb{width:min(82vw,260px);box-shadow:0 0 0 100vmax rgba(0,0,0,0)}
  .sb.open{box-shadow:8px 0 28px rgba(20,28,46,.18)}
  .body{padding:14px max(14px,env(safe-area-inset-left)) calc(20px + env(safe-area-inset-bottom))
  max(14px,env(safe-area-inset-right))}
  .toast{right:max(14px,env(safe-area-inset-right));
  bottom:calc(16px + env(safe-area-inset-bottom));left:max(14px,env(safe-area-inset-left));
  text-align:center}
  /* 터치 타깃 ≥44pt(Apple HIG)/48dp(Material) */
  .ni{padding:13px 12px}
  .btn{min-height:42px}
  .btn.sm{min-height:38px;padding:8px 12px}
  .sgb{min-height:34px;padding:7px 12px}
  /* 표: 셀은 내용 크기 유지(줄바꿈 없음) + 래퍼(overflow-x:auto)가 가로 스크롤.
     (구버전 table-layout:fixed 강제는 셀 폭을 구겨 숫자가 겹치는 원인이라 제거) */
  .tbl{white-space:nowrap}
  /* 카드 헤더: 제목·부제·컨트롤이 좁으면 다음 줄로 (겹침 방지) — 태블릿부터 적용 */
  .ch{flex-wrap:wrap;row-gap:8px}
  .ch .ct{min-width:0}
  .grid{margin-top:14px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
}

/* 휴대폰 — 600px 이하: 헤더 축약 · KPI 2열 · 차트 높이 보정 */
@media(max-width:600px){
  .hd{height:calc(54px + env(safe-area-inset-top));padding-left:max(14px,env(safe-area-inset-left));
  padding-right:max(14px,env(safe-area-inset-right))}
  .hd .ttl{font-size:calc(15.5px*var(--ts-fs,1))}
  .hd .dt{display:none}                 /* 날짜시계 숨김(공간확보) */
  .hd .st #stt{display:none}            /* 연결상태 텍스트 숨김, 점만 유지 */
  .hd .st{gap:0}
  #refresh{font-size:0;padding:9px 11px;min-width:40px;justify-content:center} /* 아이콘만 */
  #refresh i{font-size:calc(14px*var(--ts-fs,1))}
  .kpis{grid-template-columns:1fr 1fr;gap:10px}
  .kpi{padding:13px 13px;gap:10px}
  .kpi .ic{width:32px;height:32px;font-size:calc(13px*var(--ts-fs,1))}
  .kpi .lab{font-size:calc(12.5px*var(--ts-fs,1))}
  .cw{height:230px;padding:12px 8px}
  .donut-w{height:184px}
  .ch{padding:13px 14px;flex-wrap:wrap;row-gap:8px}   /* 제목·컨트롤 겹침 방지: 줄바꿈 허용 */
  .ch .ct{font-size:calc(14.5px*var(--ts-fs,1));min-width:0}
  .kpi>div{min-width:0}
  .kpi .v{font-size:calc(21px*var(--ts-fs,1));letter-spacing:-.3px;overflow-wrap:anywhere}
  .kpi .s{overflow-wrap:anywhere}
  .tbl th,.tbl td{padding:11px 13px}
  .form{padding:15px;gap:12px}
  .fact{flex-direction:column-reverse}
  .fact .btn{width:100%;justify-content:center}
  /* iOS 입력 포커스 시 자동 줌 방지 — 폰트 ≥16px */
  .fld input,.fld select{font-size:calc(16px*var(--ts-fs,1));padding:12px 13px}
  .btn,.btn.sm,.sgb,.ni{font-size:calc(14px*var(--ts-fs,1))}
}

/* 소형 폰 — 380px 이하: KPI 1열 */
@media(max-width:380px){
  .kpis{grid-template-columns:1fr}
  .hd .ttl{font-size:calc(14.5px*var(--ts-fs,1))}
  .body{padding:11px 11px calc(16px + env(safe-area-inset-bottom))}
}

/* 가로 모드 낮은 높이 — 차트 압축 */
@media(max-height:480px) and (orientation:landscape){
  .cw{height:200px}.donut-w{height:170px}
}
</style>
<!-- TS_V3_HEAD_START -->
<style id="ts-renewal-theme">/* Trading Suite Professional — presentation-only overlay, 2026-09-22.
   Keep original CSS first. No controls, columns or status messages are hidden. */
:root{--blue:#315bd9;--blue-s:#edf2ff;--indigo:#7264cf;--green:#158466;--green-s:#edf8f3;--red:#d44d59;--red-s:#fff0f2;--amber:#aa6b16;--amber-s:#fff6e8;--bg:#f4f6fa;--card:#fff;--line:#e5eaf2;--c0:#18283f;--c1:#52617a;--c2:#728098;--c3:#b7c2d4;--sb:226px;--hd:76px;--rd:14px;--sh:0 3px 16px rgba(22,40,72,.025)}
body{font-family:Inter,'Noto Sans KR','Apple SD Gothic Neo','Malgun Gothic',sans-serif;font-size:calc(14px*var(--ts-fs,1));line-height:1.6;letter-spacing:-.25px}
.sb{background:#152239;border-right:1px solid #253650;color:#b5c3d8}
.brand{padding:26px 20px;border-color:#2b3a52;gap:12px;min-height:100px}
.brand b{color:#f4f7fd;font-size:calc(15px*var(--ts-fs,1));letter-spacing:.15px;font-weight:700}
.brand small{color:#93a8c8;font-size:calc(9px*var(--ts-fs,1));letter-spacing:2.6px;margin-top:4px}
.brand .m{background:#3966dd;border-radius:10px;box-shadow:0 4px 14px #0b162b40}
.nav{padding:22px 13px;scrollbar-color:#4e607a transparent}
.ni{font-size:calc(13px*var(--ts-fs,1));font-weight:500;padding:13px 14px;min-height:46px;margin-bottom:5px;color:#b6c5dd;border-radius:8px;transition:background .15s}
.ni:hover{background:#223550;color:#fff}.ni.on{background:#2c4265;box-shadow:inset 3px 0 #83a4ff;color:#fff}
.ni .ch{border:0;padding:0}.ni .i{font-size:calc(15px*var(--ts-fs,1))}.ni:nth-child(9){margin-top:24px}
.sbhelp{margin:12px 14px 20px;padding:16px;border-radius:10px;background:#1d2e47;color:#a9bcd8;font-size:calc(11px*var(--ts-fs,1));border:1px solid #2b3d59;line-height:1.9}
.sbhelp b{color:#dce6f8;font-size:calc(12px*var(--ts-fs,1));font-weight:600}
.hd{padding-left:30px;padding-right:30px;gap:16px;background:rgba(255,255,255,.97);box-shadow:none}
.hd .ttl{font-size:calc(21px*var(--ts-fs,1));font-weight:700;letter-spacing:-.8px}.hd .dt{font-size:calc(11px*var(--ts-fs,1));color:var(--c2)}
.hd .st{font-size:calc(11px*var(--ts-fs,1));font-weight:500;background:#f3f7f6;border:1px solid #e4eeea;padding:6px 10px;border-radius:6px}
.hd .st.off{background:var(--red-s);border-color:#f3dce0}.hd .st .d{width:6px;height:6px;box-shadow:none}
.body{width:100%;max-width:1800px;margin:0 auto;padding:28px 30px 44px}
.btn{min-height:40px;font-size:calc(12px*var(--ts-fs,1));padding:9px 14px;border-radius:7px;font-weight:600;border-color:#dce3ee}
.btn.sm{min-height:36px;font-size:calc(12px*var(--ts-fs,1))}.btn.p{box-shadow:0 2px 4px #315bd918}.btn.dg{border-color:#f2ccd3}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #85a8ff;outline-offset:3px}
input,select,textarea{max-width:100%}button:disabled{opacity:.55;cursor:not-allowed}
.grid{gap:20px;margin-top:20px;min-width:0;align-items:start}.grid>*{min-width:0}
.g-3-1{grid-template-columns:minmax(0,1.9fr) minmax(280px,1fr)}.g-2{grid-template-columns:repeat(2,minmax(0,1fr))}
.card{min-width:0;overflow-x:auto;overscroll-behavior-x:contain;box-shadow:var(--sh)}
.ch{padding:18px 21px;gap:12px;flex-wrap:wrap;border-bottom:1px solid #edf0f5;min-width:0}
.ch .ct{font-size:calc(14px*var(--ts-fs,1));font-weight:700;min-width:0;letter-spacing:-.35px}.ch .ct i{color:#7b90b4;font-size:calc(13px*var(--ts-fs,1));margin-right:9px}
.ch .lk{font-size:calc(11px*var(--ts-fs,1))}.ch .seg{flex-shrink:0;max-width:100%;overflow-x:auto}
#page>.ch{min-height:65px;box-shadow:none!important;padding:13px 18px}
.kpis{grid-template-columns:repeat(6,minmax(0,1fr));gap:12px}
.kpi{padding:20px 17px;position:relative;min-width:0;display:block;min-height:148px;box-shadow:none}
.kpi>.ic{position:absolute;right:15px;top:16px;width:25px;height:25px;background:transparent!important;color:#91a2bf;font-size:calc(13px*var(--ts-fs,1))}
.kpi .lab{padding-right:23px;font-size:calc(12px*var(--ts-fs,1));color:#64758e;font-weight:500;line-height:1.5}
.kpi .v{font-size:calc(clamp(20px,1.65vw,29px)*var(--ts-fs,1));font-weight:700;letter-spacing:-1px;line-height:1.25;margin-top:18px;overflow-wrap:anywhere}
.kpi .s{font-size:calc(11px*var(--ts-fs,1));color:#728098;margin-top:10px;line-height:1.6;overflow-wrap:anywhere}
.kpi.hi{background:linear-gradient(125deg,#ecf1ff,#f6f8ff);border-color:#dbe4ff;box-shadow:none}
.kpi.hi .v{color:#274ea6}.kpi .v.up{color:var(--green)}.kpi .v.down{color:var(--red)}
.seg{background:#f0f3f8;border:1px solid #e9edf4;padding:3px;border-radius:7px;gap:3px}
.sgb{padding:7px 11px;font-size:calc(11px*var(--ts-fs,1));min-height:33px;white-space:nowrap;border-radius:5px;color:#67778f}
.sgb.on{color:#23479f;box-shadow:0 1px 5px #1c366315;background:white}
.cw{height:320px;padding:18px 20px 12px}.donut-w{height:214px;margin-top:18px}.donut-c s{text-decoration:none;font-size:calc(11px*var(--ts-fs,1))}.donut-c b{font-size:calc(24px*var(--ts-fs,1));letter-spacing:-.7px}
.lg{padding:20px 22px 24px;gap:11px}.lg .r{font-size:calc(12px*var(--ts-fs,1))}.lg .r .p{font-size:calc(11px*var(--ts-fs,1))}
.bars{padding:25px 23px;gap:22px}.bar{font-size:calc(12px*var(--ts-fs,1))}.bar .t{margin-bottom:10px;gap:10px}.bar .tr{height:7px;background:#edf1f7}
#slist>div{min-height:63px;flex-wrap:wrap;row-gap:6px}#slist b{font-weight:600;min-width:105px}
.tbl{font-size:calc(12px*var(--ts-fs,1));white-space:nowrap}.tbl th{background:#f7f9fc;color:#718198;font-weight:500;font-size:calc(11px*var(--ts-fs,1));padding:13px 20px;letter-spacing:0}
.tbl td{padding:16px 20px;border-bottom:1px solid #edf1f7;line-height:1.55}.tbl td b{font-weight:600}.tbl tbody tr:hover{background:#f7faff}
.bdg{font-size:calc(10px*var(--ts-fs,1));font-weight:600;padding:4px 8px;border-radius:5px;white-space:nowrap;line-height:1.5}.bdg::before{width:5px;height:5px}
.tag{font-size:calc(10px*var(--ts-fs,1));border-radius:4px;padding:4px 7px}.up,.dn{font-weight:600}
.al{padding:16px 21px;font-size:calc(12px*var(--ts-fs,1))}.al .at{font-size:calc(10px*var(--ts-fs,1))}.empty{min-height:240px}.empty .s{line-height:1.9}
.form{padding:23px;gap:19px}.fld{min-width:0}.fld label{font-size:calc(12px*var(--ts-fs,1));font-weight:500;margin-bottom:8px}.fld input,.fld select{border-color:#dce3ed;border-radius:7px;font-size:calc(14px*var(--ts-fs,1));min-height:44px}
.tip{font-size:calc(12px*var(--ts-fs,1));line-height:1.85;border:1px solid #e1e8fa;margin:0 22px 20px;background:#f4f7ff}.fnote{font-size:calc(12px*var(--ts-fs,1));line-height:1.8}
.fact{flex-wrap:wrap}.hl{line-height:1.8;border-radius:8px}.vtblk{padding:20px}.trc{width:36px;height:36px;border-radius:6px}.vth{flex-wrap:wrap}
.toast{max-width:min(520px,calc(100vw - 28px));font-size:calc(12px*var(--ts-fs,1));border-radius:8px}.muted{line-height:1.8}
@media(min-width:981px) and (max-width:1350px){:root{--sb:208px}.body{padding:24px}.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.kpi{min-height:138px}.kpi .v{font-size:calc(26px*var(--ts-fs,1))}.g-3-1{grid-template-columns:minmax(0,1.6fr) minmax(260px,1fr)}}
@media(max-width:980px){.sb{width:min(82vw,280px)}.body{padding:22px max(20px,env(safe-area-inset-left)) calc(30px + env(safe-area-inset-bottom)) max(20px,env(safe-area-inset-right))}.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.kpi .v{font-size:calc(25px*var(--ts-fs,1))}.g-3-1,.g-2{grid-template-columns:1fr}.hd{padding-left:20px;padding-right:20px;gap:10px}.hd .ttl{font-size:calc(19px*var(--ts-fs,1))}.btn,.btn.sm,.sgb,.hamb,.ni{min-height:44px}.cw{height:310px}.tbl td{padding:15px 18px}.ch .seg{flex-wrap:wrap}.form{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.hd{height:auto;min-height:70px;padding:10px max(12px,env(safe-area-inset-left));padding-top:calc(10px + env(safe-area-inset-top));gap:7px;flex-wrap:wrap}.hd .ttl{font-size:calc(17px*var(--ts-fs,1))}.hd .dt{display:block;order:5;flex-basis:100%;font-size:calc(10px*var(--ts-fs,1));text-align:right;line-height:1.3}.hd .st #stt{display:inline}.hd .st{padding:5px 7px;font-size:calc(10px*var(--ts-fs,1));gap:5px}.hd .sp{min-width:0}#refresh{font-size:0;min-height:44px;min-width:44px;padding:9px}#refresh i{font-size:calc(14px*var(--ts-fs,1))}.hamb{margin-left:0;min-width:36px}.body{padding:16px max(12px,env(safe-area-inset-left)) calc(24px + env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-right))}.kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.kpi{padding:16px 13px;min-height:135px}.kpi .v{font-size:calc(23px*var(--ts-fs,1));margin-top:17px}.kpi .s{font-size:calc(10px*var(--ts-fs,1))}.kpi .lab{font-size:calc(11px*var(--ts-fs,1))}.kpi>.ic{right:12px;top:11px;width:21px}.grid{gap:14px;margin-top:14px}.ch{padding:15px;gap:10px}.ch .ct{font-size:calc(13px*var(--ts-fs,1));flex-basis:auto}.ch .seg{width:100%;flex-wrap:wrap}.sgb{font-size:calc(12px*var(--ts-fs,1));flex:1;padding:8px}.cw{height:265px;padding:12px 8px}.tbl th,.tbl td{padding:13px 15px}.form{grid-template-columns:1fr;padding:18px;gap:15px}.fld input,.fld select,input,select,textarea{font-size:calc(16px*var(--ts-fs,1))}.fact .btn{min-height:46px}.tip{margin:0 15px 16px}.lg{padding:18px 16px}.lg .r{font-size:calc(11px*var(--ts-fs,1))}#page>.ch{flex-wrap:wrap!important;gap:8px;padding:13px!important}#page>.ch>span:nth-child(2){flex-basis:70%!important;white-space:normal!important;overflow:visible!important}#acctSeg{width:100%;justify-content:space-between}.btn.sm{font-size:calc(12px*var(--ts-fs,1))}.trc{width:36px;height:36px}}
@media(max-width:359px){.kpis{grid-template-columns:1fr}.hd .st{font-size:calc(9px*var(--ts-fs,1))}.hd .ttl{font-size:calc(15px*var(--ts-fs,1))}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
@media print{.sb,.hamb,.scrim,#refresh{display:none!important}.mn{margin:0}.hd{position:static}.body{padding:0;max-width:none}.grid{display:block}.card{break-inside:avoid;margin-top:12px;overflow:visible}.kpis{grid-template-columns:repeat(3,1fr)}body{background:white}}
/* Existing inline multi-column forms need explicit mobile overrides. */
@media(max-width:980px){.form[style*="grid-template-columns"]{grid-template-columns:repeat(2,minmax(0,1fr))!important}}
@media(max-width:600px){.form[style*="grid-template-columns"]{grid-template-columns:minmax(0,1fr)!important}#vrBody input,#vrBody select{font-size:calc(16px*var(--ts-fs,1))!important;min-height:44px}#bjBody>div[style*="display:flex"]{flex-wrap:wrap}}
.kpis:has(>.kpi:last-child:nth-child(3)){grid-template-columns:repeat(3,minmax(0,1fr))}
.kpis:has(>.kpi:last-child:nth-child(4)){grid-template-columns:repeat(4,minmax(0,1fr))}
@media(max-width:980px){.kpis:has(>.kpi:last-child:nth-child(4)){grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.kpis:has(>.kpi:last-child:nth-child(3)){grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:359px){.kpis:has(>.kpi:last-child:nth-child(3)),.kpis:has(>.kpi:last-child:nth-child(4)){grid-template-columns:1fr}}</style>
<style id="ts-workspace-css">/* V3: editorial portfolio workspace. All operational pages remain available. */
:root{--blue:#326957;--blue-s:#edf5f1;--green:#1b8062;--green-s:#eaf5ee;--bg:#f4f5f3;--line:#e2e7e2;--c0:#23372e;--c1:#56675c;--c2:#7b897f;--sb:204px;--rd:12px;--sh:none}
body.ws-v3{background:#f4f5f3;color:#23372e;font-family:'Noto Sans KR','Malgun Gothic',system-ui,sans-serif}.ws-v3 .sb{background:#fafbf8;border-color:#e0e5de;color:#5b6d60}.ws-v3 .brand{padding:30px 18px;border:0;min-height:115px}.ws-v3 .brand b{color:#263d30;font-size:calc(13px*var(--ts-fs,1));letter-spacing:1px}.ws-v3 .brand small{color:#829184;font-size:calc(8px*var(--ts-fs,1))}.ws-v3 .brand .m{background:#234e3a;border-radius:50%;width:31px;height:31px;box-shadow:none}.ws-v3 .nav{padding:10px 13px}.ws-v3 .ni{color:#67766b;font-size:calc(12px*var(--ts-fs,1));font-weight:500;border-radius:7px;margin-bottom:7px;padding:13px 12px}.ws-v3 .ni.on{background:#e6ede4;color:#204b31;box-shadow:none;font-weight:700}.ws-v3 .ni.on .i{color:#28603d}.ws-v3 .ni:hover{background:#edf1e9;color:#213c29}.ws-v3 .ni .ch{display:none}.ws-v3 .ni:nth-child(9){border-top:1px solid #e1e7de;border-radius:0;padding-top:22px}.ws-v3 .sbhelp{background:#eef2e9;border:0;color:#6b7b68;font-size:calc(10px*var(--ts-fs,1));margin:15px 14px 23px}.ws-v3 .sbhelp b{color:#445f43;font-size:calc(11px*var(--ts-fs,1))}.ws-v3 .hd{background:var(--bg);border:0;height:66px;box-shadow:none;padding-left:36px;padding-right:36px}.ws-v3 .hd .ttl{font-size:calc(12px*var(--ts-fs,1));color:#7d8b7c;font-weight:500}.ws-v3 .hd .st{background:#eaf0e7;border:0;color:#466649}.ws-v3 .hd .st.off{background:#fff0ed;color:#aa413b}.ws-v3 .hd .dt{font-size:calc(10px*var(--ts-fs,1))}.ws-v3 .btn{border-radius:6px}.ws-v3 .btn.p{background:#315e45;border-color:#315e45;color:white;box-shadow:none}.ws-v3 .btn.p:hover{background:#244c35}.ws-v3 .body{padding:10px 36px 44px;max-width:1750px}.ws-heading{display:flex;justify-content:space-between;align-items:center;gap:24px;margin:10px 0 28px}.ws-overline{font-size:calc(9px*var(--ts-fs,1));font-weight:600;letter-spacing:2.5px;color:#889587;display:block}.ws-heading h1{font-size:calc(31px*var(--ts-fs,1));letter-spacing:-1.2px;font-weight:600;line-height:1.4;margin:9px 0 5px}.ws-heading p{font-size:calc(11px*var(--ts-fs,1));color:#7e8b80}.ws-account{padding:0!important;margin:0!important;background:transparent!important;border:0!important;border-radius:0!important;box-shadow:none!important;flex-direction:column;align-items:flex-end;gap:6px;max-width:390px}.ws-account>.ct{display:none}.ws-account>span:nth-child(2){font-size:calc(10px*var(--ts-fs,1))!important;order:2;flex:none!important;color:#8a958c}.ws-account .seg{background:#e8ece5;border-color:#e2e7df;padding:4px}.ws-account .sgb{min-height:32px;padding:7px 17px;font-size:calc(11px*var(--ts-fs,1))}.ws-account .sgb.on{color:#305439}.ws-main{display:grid;grid-template-columns:minmax(0,1fr) 316px;gap:22px;align-items:stretch}.ws-wealth{background:#fff;border:1px solid #e0e6de;border-radius:16px;min-width:0;overflow:hidden}.ws-wealth-top{display:flex;align-items:center;justify-content:space-between;padding:27px 28px 0;gap:15px}.ws-v3 .ws-wealth .kpi{border:0;min-height:0;background:transparent;box-shadow:none;padding:0;display:block}.ws-v3 .ws-wealth .kpi>.ic{display:none}.ws-v3 .ws-main-asset .lab{font-size:calc(11px*var(--ts-fs,1));color:#7e8b81;padding:0}.ws-v3 .ws-main-asset .v{font-size:calc(46px*var(--ts-fs,1));letter-spacing:-2.4px;font-weight:500;line-height:1.25;color:#233f30;margin:9px 0 0;overflow-wrap:anywhere}.ws-v3 .ws-main-asset .s{font-size:calc(10px*var(--ts-fs,1));margin-top:6px;color:#8a978e}.ws-v3 .ws-main-return{text-align:right;flex-shrink:0}.ws-v3 .ws-main-return .lab{font-size:calc(10px*var(--ts-fs,1));color:#8a958d;padding:0}.ws-v3 .ws-main-return .v{font-size:calc(24px*var(--ts-fs,1));letter-spacing:-.8px;margin-top:7px;font-weight:500}.ws-v3 .ws-main-return .s{font-size:calc(10px*var(--ts-fs,1));margin-top:7px}.ws-chart-context{font-size:calc(9px*var(--ts-fs,1));color:#8d998f;padding:19px 28px 0;line-height:1.6}.ws-wealth-chart{border:0!important;border-radius:0!important;overflow:visible!important;background:transparent}.ws-wealth-chart>.ch{border:0;padding:16px 27px 0;justify-content:flex-end}.ws-wealth-chart>.ch>.ct{font-size:calc(11px*var(--ts-fs,1));font-weight:500;color:#8b998e}.ws-wealth-chart>.ch>.ct i{display:none}.ws-wealth-chart .seg{background:#f7f8f5;border:0}.ws-wealth-chart .sgb{min-height:28px;font-size:calc(10px*var(--ts-fs,1));padding:5px 10px}.ws-wealth-chart .sgb.on{color:#2d6547;box-shadow:none;background:#eaf2e7}.ws-wealth-chart .cw{height:240px;padding:10px 22px 5px}.ws-supporting{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-top:1px solid #edf0eb;padding:20px 28px;gap:20px;margin-top:7px}.ws-v3 .ws-supporting .kpi .lab{font-size:calc(10px*var(--ts-fs,1));padding:0}.ws-v3 .ws-supporting .kpi .v{font-size:calc(21px*var(--ts-fs,1));font-weight:500;margin-top:7px;letter-spacing:-.7px}.ws-v3 .ws-supporting .kpi .s{font-size:calc(9px*var(--ts-fs,1));line-height:1.65;margin-top:5px}.ws-rail{display:flex;flex-direction:column;gap:16px;min-width:0}.ws-operation{background:#fff;border:1px solid #e0e6de;border-radius:14px;padding:21px 20px;flex:1;min-width:0}.ws-section-head{display:flex;justify-content:space-between;align-items:center;gap:8px}.ws-section-head h2{font-size:calc(13px*var(--ts-fs,1));font-weight:600}.ws-link{border:0;background:none;font:inherit;font-size:calc(10px*var(--ts-fs,1));color:#799080;cursor:pointer;white-space:nowrap}.ws-live{display:flex;align-items:center;gap:6px;font-size:calc(10px*var(--ts-fs,1));color:#658069;margin:15px 0 14px}.ws-live-dot{width:5px;height:5px;border-radius:50%;background:#65946d}.ws-live.attention{color:#aa413b}.ws-live.attention .ws-live-dot{background:#aa413b}.ws-v3 .ws-strategy-count{min-height:0;padding:0;margin:0 0 9px;border:0;box-shadow:none;background:none;display:flex;gap:12px}.ws-strategy-count>.ic{display:none}.ws-v3 .ws-strategy-count .lab{font-size:calc(10px*var(--ts-fs,1));padding:0}.ws-v3 .ws-strategy-count .v{font-size:calc(26px*var(--ts-fs,1));font-weight:500;letter-spacing:-.9px;line-height:1.2;margin-top:5px}.ws-v3 .ws-strategy-count .s{font-size:calc(10px*var(--ts-fs,1));margin-top:5px}.ws-strategy-list{border:0!important;overflow:visible!important}.ws-strategy-list>.ch{display:none}.ws-v3 #slist>div{padding:11px 0!important;font-size:calc(10px*var(--ts-fs,1))!important;gap:6px!important;min-height:48px;flex-wrap:wrap}.ws-v3 #slist>div>b{font-size:calc(11px*var(--ts-fs,1));font-weight:500;min-width:100px}.ws-v3 #slist .bdg{font-size:calc(9px*var(--ts-fs,1));padding:3px 6px;margin-left:0!important;border-radius:3px}.ws-v3 #slist .up,.ws-v3 #slist .dn{font-size:calc(10px*var(--ts-fs,1))}.ws-v3 #slist>div>i{width:5px!important;height:5px!important}.ws-wide-link{border:0;border-top:1px solid #e9eee7;background:none;width:100%;text-align:left;font:inherit;color:#50745a;font-size:calc(11px*var(--ts-fs,1));padding:16px 0 0;cursor:pointer;margin-top:8px}.ws-next{border-radius:12px;background:#e8ede1;padding:22px;color:#334e34;position:relative}.ws-next .ws-overline{font-size:calc(8px*var(--ts-fs,1));color:#809276}.ws-next h2{font-size:calc(17px*var(--ts-fs,1));font-weight:500;margin:10px 0 5px;letter-spacing:-.5px}.ws-next p{font-size:calc(10px*var(--ts-fs,1));color:#819078;margin-bottom:16px}.ws-primary{background:#315a3e;color:#f7faf5;border:0;border-radius:5px;padding:10px 14px;font:inherit;font-size:calc(11px*var(--ts-fs,1));width:100%;text-align:left;cursor:pointer;min-height:40px}.ws-primary:hover{background:#23482f}.ws-alert-row{margin:18px 0 24px}.ws-alert-card{background:#eff3ea;border:1px solid #e0e7d9!important;border-radius:9px!important;display:flex;align-items:stretch}.ws-alert-card>.ch{padding:12px 18px;border:0;flex-shrink:0}.ws-alert-card>.ch .ct{font-size:calc(11px*var(--ts-fs,1));color:#64775b}.ws-alert-card>.ch .ct i{color:#7b9271;font-size:calc(11px*var(--ts-fs,1))}.ws-alert-card #palert{flex:1}.ws-alert-card .al{padding:12px 14px;font-size:calc(11px*var(--ts-fs,1));align-items:center;border-color:#dde6d5}.ws-alert-card .at{font-size:calc(9px*var(--ts-fs,1))}.ws-activity{display:grid;grid-template-columns:minmax(0,1fr);gap:20px}.ws-activity .card{border:1px solid #e0e6de;border-radius:12px;box-shadow:none;background:white}.ws-activity .ch{padding:19px 22px}.ws-activity .ch .ct{font-size:calc(13px*var(--ts-fs,1));font-weight:500}.ws-activity .ch .ct i{display:none}.ws-v3 .tbl th{font-size:calc(10px*var(--ts-fs,1));background:#f8faf6;color:#81917f;font-weight:500;padding:12px 20px;border-color:#ecf0e8}.ws-v3 .tbl td{font-size:calc(12px*var(--ts-fs,1));padding:16px 20px;border-color:#edf0e9}.ws-v3 .tbl td b{font-weight:500}.ws-v3 .tbl tr:hover{background:#f5f8f2}.ws-v3 .hl.g{background:#f2f7ee;border-color:#e2ebda;color:#5b7753}.ws-disclosure{margin-top:20px;border:1px solid #e0e6de;background:#fff;border-radius:12px;overflow:hidden}.ws-disclosure>summary{cursor:pointer;display:flex;align-items:center;justify-content:space-between;list-style:none;padding:22px}.ws-disclosure>summary::-webkit-details-marker{display:none}.ws-disclosure summary b{display:block;font-size:calc(13px*var(--ts-fs,1));font-weight:500}.ws-disclosure summary small{display:block;font-size:calc(10px*var(--ts-fs,1));color:#8b978b;margin-top:5px}.ws-expand{font-size:calc(22px*var(--ts-fs,1));color:#7c9277;font-weight:300}.ws-disclosure[open] .ws-expand{transform:rotate(45deg)}.ws-disclosure-body{padding:0 18px 18px;display:grid;gap:18px}.ws-analysis-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;min-width:0}.ws-analysis-grid>.card{min-width:0}.ws-v3 .form{gap:20px}.ws-v3 .fld input:focus,.ws-v3 .fld select:focus{border-color:#6f9570;box-shadow:0 0 0 3px #edf3e7}.ws-v3 .tip{background:#f0f5eb;border-color:#dfe9d7}.ws-mobile{display:none}.ws-v3 .toast{background:#243e2d}
@media(min-width:1600px){.ws-main{grid-template-columns:minmax(0,1fr) 345px}.ws-wealth-chart .cw{height:275px}.ws-v3 .ws-main-asset .v{font-size:calc(53px*var(--ts-fs,1))}.ws-wealth-top{padding-top:33px}.ws-activity{grid-template-columns:minmax(0,1fr)} }
@media(max-width:1200px){.ws-v3 .body{padding-left:23px;padding-right:23px}.ws-main{grid-template-columns:minmax(0,1fr) 275px;gap:16px}.ws-operation{padding:18px 16px}.ws-v3 .ws-main-asset .v{font-size:calc(36px*var(--ts-fs,1))}.ws-v3 .ws-main-return .v{font-size:calc(22px*var(--ts-fs,1))}.ws-wealth-top{padding:25px 22px 0}.ws-supporting{padding:19px 22px;gap:13px}.ws-v3 .ws-supporting .kpi .v{font-size:calc(18px*var(--ts-fs,1))}.ws-heading{gap:14px}.ws-heading h1{font-size:calc(29px*var(--ts-fs,1))}}
@media(max-width:980px){.ws-v3 .body{padding-left:22px;padding-right:22px;padding-bottom:90px}.ws-v3 .hd{padding-left:20px;padding-right:20px}.ws-main{grid-template-columns:minmax(0,1fr) 285px}.ws-v3 .ws-main-asset .v{font-size:calc(36px*var(--ts-fs,1))}.ws-v3 .ws-main-return .v{font-size:calc(22px*var(--ts-fs,1))}.ws-heading h1{font-size:calc(28px*var(--ts-fs,1))}.ws-heading{margin-bottom:22px}.ws-mobile{display:none}.ws-v3 .ws-mobile{position:fixed;bottom:0;left:0;right:0;z-index:24;display:flex;background:#fafcf8;border-top:1px solid #dde5d7;padding:8px 10px calc(8px + env(safe-area-inset-bottom));box-shadow:0 -5px 25px #243e2d06}.ws-mobile button{background:none;border:0;color:#8c9889;font:inherit;font-size:calc(10px*var(--ts-fs,1));display:flex;flex:1;align-items:center;justify-content:center;flex-direction:column;gap:5px;min-height:44px;cursor:pointer}.ws-mobile i{font-size:calc(16px*var(--ts-fs,1))}.ws-mobile button.selected{color:#2c623a;font-weight:700}.ws-v3 .toast{bottom:calc(80px + env(safe-area-inset-bottom))}.ws-v3 .ws-wealth-chart .sgb{min-height:40px}}
@media(max-width:760px){.ws-main{grid-template-columns:1fr}.ws-rail{display:grid;grid-template-columns:1.3fr 1fr;gap:14px}.ws-operation{padding:19px}.ws-next{display:flex;flex-direction:column;justify-content:center}.ws-v3 .ws-main-asset .v{font-size:calc(42px*var(--ts-fs,1))}.ws-v3 .ws-main-return .v{font-size:calc(25px*var(--ts-fs,1))}.ws-wealth-chart .cw{height:240px}.ws-heading{align-items:flex-start}.ws-account{max-width:320px}.ws-account .sgb{padding:8px 13px}.ws-analysis-grid{grid-template-columns:1fr}}
@media(max-width:600px){.ws-v3 .hd{height:auto;min-height:55px;padding:8px 14px;gap:9px}.ws-v3 .hd .ttl{font-size:calc(11px*var(--ts-fs,1))}.ws-v3 .hd .dt{font-size:calc(9px*var(--ts-fs,1))}.ws-v3 .body{padding:9px 14px 94px}.ws-heading{display:block;margin:10px 0 17px}.ws-heading h1{font-size:calc(27px*var(--ts-fs,1));margin-top:7px}.ws-heading p{font-size:calc(10px*var(--ts-fs,1))}.ws-overline{font-size:calc(8px*var(--ts-fs,1));letter-spacing:2px}.ws-account{margin:17px 0 0!important;max-width:none;width:100%;align-items:flex-start;gap:7px}.ws-account #acctSeg{width:100%;order:1}.ws-account #acctSeg .sgb{min-height:42px;font-size:calc(12px*var(--ts-fs,1));flex:1}.ws-account>span:nth-child(2){font-size:calc(9px*var(--ts-fs,1))!important;order:2;flex-basis:auto!important}.ws-wealth{border-radius:12px}.ws-wealth-top{padding:22px 19px 0;align-items:flex-start}.ws-v3 .ws-main-asset .v{font-size:calc(33px*var(--ts-fs,1));letter-spacing:-1.5px}.ws-v3 .ws-main-return .v{font-size:calc(20px*var(--ts-fs,1));letter-spacing:-.7px;margin-top:11px}.ws-v3 .ws-main-return .s{font-size:calc(9px*var(--ts-fs,1));max-width:105px}.ws-v3 .ws-main-asset .lab,.ws-v3 .ws-main-return .lab{font-size:calc(9px*var(--ts-fs,1))}.ws-v3 .ws-main-asset .s{font-size:calc(9px*var(--ts-fs,1))}.ws-chart-context{padding:15px 19px 0;font-size:calc(8px*var(--ts-fs,1))}.ws-wealth-chart>.ch{padding:12px 15px 0;gap:8px}.ws-wealth-chart>.ch>.ct{font-size:calc(10px*var(--ts-fs,1))}.ws-wealth-chart .seg{width:100%;margin-top:0}.ws-wealth-chart .sgb{padding:5px 6px;font-size:calc(11px*var(--ts-fs,1))}.ws-wealth-chart .cw{height:215px;padding:8px 9px 0}.ws-supporting{padding:17px 18px;gap:12px;margin-top:4px}.ws-v3 .ws-supporting .kpi .v{font-size:calc(17px*var(--ts-fs,1));letter-spacing:-.5px}.ws-v3 .ws-supporting .kpi .lab{font-size:calc(9px*var(--ts-fs,1))}.ws-v3 .ws-supporting .kpi .s{font-size:calc(8px*var(--ts-fs,1))}.ws-rail{display:flex;gap:13px}.ws-operation{padding:18px}.ws-strategy-count{margin-bottom:3px!important}.ws-v3 .ws-strategy-count .v{font-size:calc(23px*var(--ts-fs,1))}.ws-v3 #slist>div{min-height:47px!important}.ws-next{padding:19px;display:grid;grid-template-columns:1fr auto;column-gap:10px}.ws-next .ws-overline{grid-column:1}.ws-next h2{font-size:calc(15px*var(--ts-fs,1));grid-column:1;margin:5px 0}.ws-next p{grid-column:1;font-size:calc(9px*var(--ts-fs,1));margin:0}.ws-next .ws-primary{grid-column:2;grid-row:1/4;align-self:center;width:auto;font-size:calc(10px*var(--ts-fs,1));min-height:44px;padding:10px}.ws-alert-row{margin:14px 0 18px}.ws-alert-card{display:block}.ws-alert-card>.ch{padding:10px 13px 0}.ws-alert-card .al{padding:9px 13px 12px;font-size:calc(10px*var(--ts-fs,1))}.ws-activity{gap:15px}.ws-activity .ch{padding:16px}.ws-v3 .tbl th,.ws-v3 .tbl td{padding:13px 16px}.ws-v3 .tbl th{font-size:calc(10px*var(--ts-fs,1))}.ws-v3 .tbl td{font-size:calc(11px*var(--ts-fs,1))}.ws-disclosure{margin-top:16px}.ws-disclosure>summary{padding:18px}.ws-disclosure summary b{font-size:calc(12px*var(--ts-fs,1))}.ws-disclosure summary small{font-size:calc(9px*var(--ts-fs,1))}.ws-disclosure-body{padding:0 10px 12px}.ws-analysis-grid .card>.ch .ct{font-size:calc(12px*var(--ts-fs,1))}}
@media(max-width:359px){.ws-wealth-top{flex-direction:column;gap:12px}.ws-v3 .ws-main-return{text-align:left}.ws-v3 .ws-main-return>div{display:flex;gap:7px;align-items:center}.ws-v3 .ws-main-return .v{margin:0;font-size:calc(18px*var(--ts-fs,1))}.ws-v3 .ws-main-return .s{margin:0}.ws-next{display:block}.ws-next .ws-primary{margin-top:14px;width:100%}.ws-v3 .ws-main-asset .v{font-size:calc(34px*var(--ts-fs,1))}.ws-supporting{gap:9px;padding-left:14px;padding-right:14px}.ws-v3 .ws-supporting .kpi .v{font-size:calc(15px*var(--ts-fs,1))}}
@media print{.ws-mobile,.ws-next{display:none!important}.ws-main{display:block}.ws-rail{margin-top:15px}.ws-disclosure:not([open])>.ws-disclosure-body{display:block}.ws-v3 .sb{display:none}.ws-v3 .body{padding:0}}
/* Keep trading-side colors distinct from performance colors. */
.ws-v3 .tag.buy{background:#fff0f2;color:#c23030}.ws-v3 .tag.sell{background:#edf2ff;color:#315bd9}</style>
<!-- TS_TYPE_SCALE_START -->
<style id="ts-type-scale">/* 글자 크기 단일 조절 노브 (2026-09-24).
   모든 font-size 는 calc(원본px * var(--ts-fs,1)) 로 걸려 있다.
   아래 숫자 하나만 바꾸면 화면 전체 글자 배율이 바뀐다 (1 = 리뉴얼 원본).
   이 블록을 통째로 지우면 fallback 1 이 적용돼 원본 크기로 정확히 복귀한다. */
:root{--ts-fs:1.3}</style>
<script id="ts-type-scale-js">/* 캔버스(Chart.js) 글자는 CSS 변수가 닿지 않아 JS 로 같은 배율을 적용한다. */
var TSFS=(function(){try{
  var v=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ts-fs'));
  return (isFinite(v)&&v>0)?v:1;}catch(e){return 1;}})();</script>
<!-- TS_TYPE_SCALE_END -->
<script id="ts-chart-theme">/* Visual-only Chart.js plugin. Does not wrap Chart or change business functions.
   Dataset values, labels, type, colors, fill/tension, axes, callbacks and events stay intact. */
(function () {
  'use strict';
  if (!window.Chart || window.Chart.registry.plugins.get('tsProfessionalTheme')) return;
  window.Chart.register({
    id: 'tsProfessionalTheme',
    beforeInit: function (chart) {
      var options = chart.config.options;
      var plugins = options.plugins || (options.plugins = {});
      var tooltip = plugins.tooltip || (plugins.tooltip = {});
      Object.assign(tooltip, {
        backgroundColor: '#172840', titleColor: '#f7faff', bodyColor: '#dce6fa',
        borderColor: '#334866', borderWidth: 1, cornerRadius: 9,
        padding: 13, boxPadding: 5,
        titleFont: Object.assign({}, tooltip.titleFont, {size: 12*TSFS, weight: '600'}),
        bodyFont: Object.assign({}, tooltip.bodyFont, {size: 12*TSFS})
      });
      if (plugins.legend && plugins.legend.labels) {
        plugins.legend.labels.padding = 18;
        plugins.legend.labels.color = '#61718b';
        plugins.legend.labels.font = Object.assign({}, plugins.legend.labels.font, {size: 11*TSFS});
      }
      var scales = options.scales || {};
      Object.keys(scales).forEach(function (key) {
        var scale = scales[key];
        if (scale.ticks) {
          scale.ticks.color = '#718198';
          scale.ticks.font = Object.assign({}, scale.ticks.font, {size: 11*TSFS});
        }
        if (scale.grid && scale.grid.display !== false) scale.grid.color = '#edf1f7';
      });
    },
    afterDatasetsDraw: function (chart) {
      // Hover guide only: no dataset/options/event mutation.
      if (chart.config.type !== 'line' || !chart.tooltip) return;
      var active = chart.tooltip.getActiveElements();
      if (!active.length || !chart.chartArea) return;
      var x = active[0].element.x, ctx = chart.ctx, area = chart.chartArea;
      ctx.save();
      try {
        ctx.beginPath(); ctx.setLineDash([3, 4]); ctx.lineWidth = 1;
        ctx.strokeStyle = '#9badcc'; ctx.moveTo(x, area.top);
        ctx.lineTo(x, area.bottom); ctx.stroke();
      } finally { ctx.restore(); }
    }
  });
}());</script>
<!-- TS_V3_HEAD_END -->
</head><body>
<div class="wrap">
  <aside class="sb" id="sb">
    <div class="brand"><div class="m"><i class="fa-solid fa-bolt"></i></div>
      <div><b>TRADING SUITE</b><small>AUTOTRADE</small></div></div>
    <nav class="nav" id="nav"></nav>
    <div class="sbhelp"><b>단일 계좌 멀티전략</b>공용 계좌 69567573<br><span id="sbst">로딩…</span></div>
  </aside>
  <div class="scrim" id="scrim"></div>
  <main class="mn">
    <header class="hd">
      <button class="hamb" id="hamb"><i class="fa-solid fa-bars"></i></button>
      <div class="ttl" id="ttl">대시보드</div><div class="sp"></div>
      <div class="dt" id="dt"></div>
      <div class="st" id="st"><span class="d"></span><span id="stt">정상 운영중</span></div>
      <button class="btn" id="refresh"><i class="fa-solid fa-rotate"></i> 새로고침</button>
    </header>
    <div class="body" id="page"><div class="muted">불러오는 중…</div></div>
  </main>
</div>
<div class="toast" id="toast"></div>
<script>
var STRATS=__STRATS__;var PAGE='dash';var MET=null;var SER=null;var R1='3M';
var VRSEL='';
var C1=null,C2=null,C3=null;var PAL=['#2f6bff','#16a34a','#6c5ce7','#d97706','#e5484d'];
var BARPAL=['#16a34a','#2f6bff','#e5484d','#d97706','#6c5ce7'];
var MENU=[['dash','대시보드','fa-gauge-high'],['strat','전략 관리','fa-sliders'],
['port','포트폴리오','fa-briefcase'],['order','주문/체결','fa-receipt'],
['risk','리스크 관리','fa-shield-halved'],['perf','성과 분석','fa-chart-line'],
['vr','VR (NH)','fa-scale-balanced'],
['blog','매매일지','fa-book'],
['mon','모니터링','fa-desktop'],['sys','시스템 설정','fa-gear']];
var TT={dash:'대시보드',strat:'전략 관리',port:'포트폴리오',order:'주문/체결',
risk:'리스크 관리',perf:'성과 분석',vr:'VR (NH)',blog:'매매일지',mon:'모니터링',sys:'시스템 설정'};
function $(i){return document.getElementById(i);}
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,function(m){
 return {'&':'&amp;','<':'&lt;','>':'&gt;'}[m];});}
function money(n,d){return (n==null||n==='')?'—':'$'+Number(n).toLocaleString(undefined,
 {maximumFractionDigits:d==null?0:d});}
function sM(n){if(n==null)return '<span style="color:#9aa3b2">—</span>';
 return '<span class="'+(n>=0?'up':'dn')+'">'+(n>=0?'+':'-')+'$'+
 Number(Math.abs(n)).toLocaleString(undefined,{maximumFractionDigits:0})+'</span>';}
function sP(n){if(n==null)return '<span style="color:#9aa3b2">—</span>';
 return '<span class="'+(n>=0?'up':'dn')+'">'+(n>=0?'+':'')+Number(n).toFixed(2)+'%</span>';}
function toast(m){var t=$('toast');t.textContent=m;t.classList.add('s');
 setTimeout(function(){t.classList.remove('s');},2600);}
function api(m,u,b){return fetch(u,{method:m,headers:{'Content-Type':'application/json'},
 body:b?JSON.stringify(b):undefined}).then(function(r){return r.json().then(function(j){
 return r.ok?j:Promise.reject(j&&(j.detail||j.message)||('HTTP '+r.status));});});}
function kindOf(k){var s=STRATS.filter(function(x){return x.key===k;})[0];return s?s.kind:'ddsop';}
function buildNav(){$('nav').innerHTML=MENU.map(function(m){
 return '<button class="ni'+(m[0]===PAGE?' on':'')+'" data-p="'+m[0]+'">'+
 '<span class="i"><i class="fa-solid '+m[2]+'"></i></span>'+m[1]+
 '<i class="fa-solid fa-chevron-right ch"></i></button>';}).join('');
 [].forEach.call($('nav').children,function(b){b.onclick=function(){go(b.dataset.p);};});}
function go(p){PAGE=p;[].forEach.call($('nav').children,function(b){
 b.classList.toggle('on',b.dataset.p===p);});$('ttl').textContent=TT[p];
 $('sb').classList.remove('open');$('scrim').classList.remove('show');render();}
function card(t,ic,body,extra){return '<div class="card"><div class="ch"><span class="ct">'+
 (ic?'<i class="fa-solid '+ic+'"></i>':'')+t+'</span>'+(extra||'')+'</div>'+body+'</div>';}
function kpi(lab,ic,icc,v,vc,s,hi){return '<div class="kpi'+(hi?' hi':'')+'"><div class="ic '+(icc||'')+'">'+
 '<i class="fa-solid '+ic+'"></i></div><div><div class="lab">'+lab+'</div>'+
 '<div class="v '+(vc||'')+'">'+v+'</div><div class="s">'+(s||'')+'</div></div></div>';}
/* ---------- 대시보드 ---------- */
var ACCT='all';   /* all | kis | nh */
function acctView(){
 /* 선택된 계좌 관점의 (account, strategies, 라벨) 반환 */
 var nh=MET.nh||{},ts=MET.toss||{};
 if(ACCT==='kis') return {a:MET.account||{},ss:MET.strategies||[],tag:'KIS',
   note:'무한매수법 · 떨사오팔 · 종사종팔'};
 if(ACCT==='nh')  return {a:nh.account||{},ss:nh.strategies||[],tag:'NH·VR',
   note:'VR 0기 · 5기 (평가금+Pool)'};
 /* 토스: 매매는 토스 앱 자동모으기가 하고 여기서는 잔고만 읽는다(조회 전용) */
 if(ACCT==='toss')return {a:ts.account||{},ss:ts.strategies||[],tag:'토스',
   note:'토스 앱 자동모으기 · 조회 전용'+(ts.ts?(' · 갱신 '+esc(String(ts.ts).slice(5,16))):'')};
 return {a:MET.combined||MET.account||{},
   ss:(MET.strategies||[]).concat(nh.strategies||[]).concat(ts.strategies||[]),tag:'전체',
   note:'KIS + NH + 토스 합산'};
}
function setAcct(v){ACCT=v;render();}
/* 토스 계좌 스냅샷 수동 갱신 — 조회 전용(주문 없음). 자동은 10분 주기. */
function tossRefresh(){toast('토스 계좌 조회 중…');TOSSSER=null;
 api('POST','/api/suite/toss/refresh').then(function(d){
  toast(d&&d.ok?('토스 갱신됨 · 평가 '+money(d.eval_usd)):'토스 갱신 실패');
  loadAll();}).catch(function(e){toast('토스 갱신 실패: '+e);});}
function pgDash(){var V=acctView(),a=V.a,au=MET.automation||{},ss=V.ss;
 var isNH=(ACCT==='nh'),isAll=(ACCT==='all'),isTS=(ACCT==='toss');
 var tsb=MET.toss||{};
 var h='<div class="ch" style="background:var(--card);border:1px solid var(--line);border-radius:var(--rd);'+
  'margin-bottom:14px;box-shadow:var(--sh);flex-wrap:nowrap">'+
  '<span class="ct" style="flex:0 0 auto"><i class="fa-solid fa-wallet"></i>계좌</span>'+
  '<span style="font-size:calc(12.5px*var(--ts-fs,1));color:var(--c2);flex:1 1 auto;min-width:0;white-space:nowrap;'+
  'overflow:hidden;text-overflow:ellipsis">'+esc(V.note)+'</span>'+
  '<div class="seg" id="acctSeg" style="flex:0 0 auto">'+
  [['all','전체'],['kis','KIS'],['nh','NH·VR'],['toss','토스']].map(function(x){
   return '<button class="sgb'+(ACCT===x[0]?' on':'')+'" onclick="setAcct(\''+x[0]+'\')">'+x[1]+'</button>';
  }).join('')+'</div></div>';
 h+='<div class="kpis">'+
  kpi('총 자산'+(isAll?'':' · '+V.tag),'fa-coins','b',money(a.total_assets),'',
   (isAll&&MET.nh&&MET.nh.eval_total?('KIS '+money((MET.account||{}).total_assets)+' + NH '+money(MET.nh.account?MET.nh.account.total_assets:MET.nh.eval_total))
    :('순투입 '+money(a.net_invested))),true)+
  (isTS
   ? kpi('보유 종목','fa-briefcase','n',(tsb.items||[]).length+' 종목','',
      (tsb.items||[]).map(function(x){return esc(x.ticker)+' '+x.qty+'주';}).join(' · ')||'조회 대기')
   : isNH
   ? kpi('VR 기수','fa-layer-group','n',(ss.length)+' 개','',
      ss.map(function(s){return (s.week_no||'-')+'주차';}).join(' · '))
   : kpi('전략 수','fa-layer-group','n',(isAll?(au.total+(MET.nh&&MET.nh.strategies?MET.nh.strategies.length:0)):au.total)+' 개','',
      '운용중 '+au.active+' · 정지 '+(au.total-au.active)+(isAll?' (+VR)':'')))+
  kpi('수익률'+(isAll?'':' · '+V.tag),'fa-chart-pie',(a.total_return_pct>=0?'g':'r'),
   (a.total_return_pct==null?'—':(a.total_return_pct>=0?'+':'')+Number(a.total_return_pct).toFixed(2)+'%'),
   (a.total_return_pct>=0?'up':'down'),
   (isNH?'평가손익 ':'누적손익 ')+((a.total_pnl||0)>=0?'+':'')+money(a.total_pnl))+
  (isTS
   ? kpi('평가손익','fa-circle-check',((a.unrealized_pnl||0)>=0?'g':'r'),
      ((a.unrealized_pnl||0)>=0?'+':'')+money(a.unrealized_pnl),((a.unrealized_pnl||0)>=0?'up':'down'),
      '매입 '+money(a.net_invested))
   : isNH
   ? kpi('V 대비','fa-scale-balanced','b',
      ((MET.nh.v_total&&MET.nh.eval_total)?((MET.nh.eval_total/MET.nh.v_total*100).toFixed(1)+'%'):'—'),'',
      '평가 '+money(MET.nh.eval_total)+' / V '+money(MET.nh.v_total))
   : kpi('실현손익'+(isAll?' · KIS':''),'fa-circle-check',((a.realized_pnl||0)>=0?'g':'r'),
      ((a.realized_pnl||0)>=0?'+':'')+money(a.realized_pnl),((a.realized_pnl||0)>=0?'up':'down'),'완료 싸이클 누적'))+
  kpi((isNH?'Pool 비중':'현금 비중'),'fa-money-bill-wave','n',
   (a.cash_ratio==null?'—':Number(a.cash_ratio).toFixed(1)+'%'),'',
   (isNH?'Pool ':'현금 ')+money(a.cash))+
  kpi('리스크'+((isNH||isTS)?' (참고)':''),'fa-shield-halved','a',
   (a.mdd_pct==null?((isNH||isTS)?'—':'수집중'):(Math.abs(a.mdd_pct)<8?'양호':Math.abs(a.mdd_pct)<15?'보통':'주의')),'',
   'MDD '+(a.mdd_pct==null?'—':Number(a.mdd_pct).toFixed(2)+'%'))+'</div>';
 /* NH 탭: 기간 대신 기수 선택 — 그래프도 VR 주차별(평가금·밴드)로 바뀐다 */
 var vrList=(isNH?((MET.nh&&MET.nh.accounts)||[]):[]);
 if(isNH&&vrList.length&&!vrList.some(function(x){return x.gisu===VRSEL;}))VRSEL=vrList[0].gisu;
 var seg=isTS?''
  :isNH
  ?('<div class="seg" id="sg">'+vrList.map(function(x){
    var nm=(((MET.nh&&MET.nh.strategies)||[]).filter(function(s){return s.strategy==='vr:'+x.gisu;})[0]||{}).display_name||x.gisu;
    return '<button class="sgb'+(VRSEL===x.gisu?' on':'')+'" data-gid="'+esc(x.gisu)+'">'+
     esc(String(nm).replace(' (NH)','').replace('VR ',''))+'</button>';}).join('')+'</div>')
  :('<div class="seg" id="sg">'+['1주','1개월','3M','6M','전체'].map(function(r){
   return '<button class="sgb'+(r===R1?' on':'')+'">'+r+'</button>';}).join('')+'</div>');
 h+='<div class="grid g-3-1">'+
  card(isTS?'토스 자산 추이':(isNH?'VR 주차별 추이':'자산 추이'),
   isTS?'fa-building-columns':(isNH?'fa-scale-balanced':'fa-chart-area'),
   '<div class="cw" id="cw1"><canvas id="c1"></canvas></div>',seg)+
  '<div class="card"><div class="ch"><span class="ct"><i class="fa-solid fa-list"></i>전략 리스트</span></div>'+
  '<div id="slist"></div></div></div>';
 h+='<div class="grid g-2">'+
  card(isNH?'기수별 비중':'전략별 손익','fa-chart-pie','<div class="donut-w"><canvas id="c2"></canvas>'+
   '<div class="donut-c"><s>'+(isNH?'평가금 합계':'실현손익 합계')+'</s><b id="dtot">—</b></div></div><div class="lg" id="lg2"></div>')+
  card(isNH?'기수별 수익률':'전략별 수익률','fa-ranking-star','<div class="bars" id="rbars"></div>')+'</div>';
 h+='<div class="grid">'+card(isNH?'기수 현황':'전략별 성과 요약','fa-table-list',
  '<div id="psum" style="overflow-x:auto"></div>')+'</div>';
 h+='<div class="grid">'+card('보유 종목','fa-wallet',
  '<div id="phold" style="overflow-x:auto"></div>',
  '<span class="lk" id="holdts" style="color:var(--c2);cursor:default"></span>')+'</div>';
 h+='<div class="grid g-2">'+card('최근 체결','fa-receipt',
  '<div id="ptr" style="overflow-x:auto"></div>')+
  card('알림','fa-bell','<div id="palert"></div>')+'</div>';
 $('page').innerHTML=h;
 $('slist').innerHTML=ss.map(function(s,i){return '<div style="display:flex;align-items:center;'+
  'gap:9px;padding:13px 18px;border-bottom:1px solid var(--line);font-size:calc(12.5px*var(--ts-fs,1))">'+
  '<i style="width:9px;height:9px;border-radius:50%;background:'+PAL[i%5]+'"></i>'+
  '<b style="flex:1">'+esc(s.display_name)+'</b>'+sP(s.return_pct)+
  '<span class="bdg '+(s.kill_switch?'stop':'run')+'" style="margin-left:8px">'+
  (s.kill_switch?'정지':'운용중')+'</span></div>';}).join('');
 renderSum(ss);renderTr(MET.recent_trades||[]);renderAlert(ss);drawDonut(ss);renderHold(MET.holdings||{});
 if(isNH&&$('ptr'))$('ptr').innerHTML='<div class="muted">VR 매매 이력은 VR (NH계좌) 메뉴의 예약 원장·체결에서 확인하세요</div>';
 if(isTS&&$('ptr'))$('ptr').innerHTML='<div class="muted">토스 매매 내역은 토스 앱에서 확인하세요 (이 화면은 잔고 조회 전용)</div>';
 if($('sg'))[].forEach.call($('sg').children,function(b){b.onclick=function(){
  [].forEach.call($('sg').children,function(x){x.classList.remove('on');});
  b.classList.add('on');
   if(ACCT==='nh')VRSEL=b.dataset.gid;else R1=b.textContent;drawLine();};});
 if(!SER){fetch('/api/suite/series').then(function(r){return r.json();}).then(function(d){
  SER=d;drawLine();});}else drawLine();
 renderBars(ss);}
function renderBars(ss){var mx=Math.max(1,Math.max.apply(null,ss.map(function(s){
  return Math.abs(s.return_pct||0);})));
 $('rbars').innerHTML=ss.map(function(s,i){var v=s.return_pct||0;
  return '<div class="bar"><div class="t"><span>'+esc(s.display_name)+'</span>'+sP(v)+
  '</div><div class="tr"><i style="width:'+(Math.abs(v)/mx*100)+'%;background:'+
  BARPAL[i%BARPAL.length]+'"></i></div></div>';}).join('');}
function renderSum(ss){
 if(ACCT==='nh'){  /* VR 기수 전용 표: V·밴드·Pool·평가 */
  $('psum').innerHTML='<table class="tbl"><thead><tr><th>기수</th><th>주차</th>'+
   '<th style="text-align:right">V(계좌)</th><th style="text-align:right">밴드</th>'+
   '<th style="text-align:right">평가금</th><th style="text-align:right">Pool</th>'+
   '<th style="text-align:right">평가수익률</th><th>주기종료</th></tr></thead><tbody>'+
   ss.map(function(s,i){return '<tr><td><span class="dn8"><i style="background:'+PAL[i%5]+
   '"></i><b>'+esc(s.display_name)+'</b></span></td><td>'+(s.week_no||'-')+'주차</td>'+
   '<td style="text-align:right">'+money(s.v)+'</td>'+
   '<td style="text-align:right" class="muted" >'+money(s.band_lo)+' ~ '+money(s.band_hi)+'</td>'+
   '<td style="text-align:right"><b>'+money(s.eval_amt)+'</b></td>'+
   '<td style="text-align:right">'+money(s.pool)+'</td>'+
   '<td style="text-align:right">'+sP(s.return_pct)+'</td>'+
   '<td>'+esc(String(s.cyc_end||'').replace(/(\d{4})(\d{2})(\d{2})/,'$1.$2.$3'))+'</td></tr>';}).join('')+
   '</tbody></table>';return;}
 $('psum').innerHTML='<table class="tbl"><thead><tr><th>전략명</th>'+
 '<th style="text-align:right">원금</th><th style="text-align:right">누적손익</th>'+
 '<th style="text-align:right">수익률</th><th style="text-align:right">승률</th>'+
 '<th style="text-align:right">보유</th><th>상태</th></tr></thead><tbody>'+
 ss.map(function(s,i){var isVr=String(s.strategy||'').indexOf('vr:')===0;
 return '<tr><td><span class="dn8"><i style="background:'+PAL[i%5]+
 '"></i><b>'+esc(s.display_name)+'</b></span></td><td style="text-align:right">'+money(s.invested)+
 '</td><td style="text-align:right">'+(isVr?sM(s.unrealized_pnl)+'<span style="color:var(--c2);font-size:calc(10px*var(--ts-fs,1))"> 평가</span>':sM(s.realized_pnl))+
 '</td><td style="text-align:right">'+
 sP(s.return_pct)+'</td><td style="text-align:right">'+(s.win_rate==null?'—':s.win_rate.toFixed(1)+'%')+
 '</td><td style="text-align:right">'+s.holdings_count+'종목</td><td><span class="bdg '+
 (s.kill_switch?'stop':'run')+'">'+(s.kill_switch?'정지':'운용중')+'</span></td></tr>';}).join('')+
 '</tbody></table>';}
function renderTr(ts){$('ptr').innerHTML=ts.length?('<table class="tbl"><thead><tr><th>일자</th>'+
 '<th>전략</th><th>종목</th><th>구분</th><th style="text-align:right">수량</th>'+
 '<th style="text-align:right">체결가</th><th style="text-align:right">금액</th></tr></thead><tbody>'+
 ts.slice(0,8).map(function(t){return '<tr><td>'+esc(t.trade_date)+'</td><td>'+esc(t.display_name)+
 '</td><td><b>'+esc(t.ticker)+'</b></td><td><span class="tag '+(t.side==='buy'?'buy">매수':'sell">매도')+
 '</span></td><td style="text-align:right">'+t.qty+'</td><td style="text-align:right">'+money(t.price,2)+
 '</td><td style="text-align:right">'+money(t.amount,2)+'</td></tr>';}).join('')+'</tbody></table>'):
 '<div class="muted">매매 내역 없음</div>';}
function renderHold(h){var its=(ACCT==='nh')?[]:((h&&h.items)||[]).slice();
 var box=$('phold');var tsEl=$('holdts');
 if(tsEl)tsEl.textContent=h&&h.ts?('갱신 '+String(h.ts).replace('T',' ').slice(0,16)):'';
 // NH(VR) 계좌 보유를 표에 합류 (캐시 스냅샷) — KIS 탭에서는 제외
 var nh=(ACCT==='kis'||ACCT==='toss')?[]:((MET&&MET.nh&&MET.nh.accounts)||[]);
 if(ACCT==='toss')its=[];
 if(ACCT==='all'||ACCT==='toss')((MET&&MET.toss&&MET.toss.items)||[]).forEach(function(x){
  if(!x.qty)return;its.push({ticker:x.ticker,name:'토스',qty:x.qty,avg_price:x.avg_price,
   now_price:x.now_price,buy_amt:x.buy_amt,eval_amt:x.eval_amt,pnl:x.pnl,pnl_rt:x.pnl_rt,
   display_name:'자동모으기 (토스)'});});
 nh.forEach(function(s){if(!s.qty)return;its.push({ticker:s.ticker,name:'NH·'+(s.name||s.gisu),
  qty:s.qty,avg_price:s.avg_price,now_price:s.now_price,buy_amt:s.buy_amt,eval_amt:s.eval_amt,
  pnl:s.pnl,pnl_rt:s.pnl_rt,display_name:(s.name||'VR')+' (NH)'});});
 if(!its.length){box.innerHTML='<div class="muted">보유 종목 없음 (또는 잔고 동기화 대기중)</div>';return;}
 var tEval=0,tPnl=0,tBuy=0;
 its.forEach(function(x){tEval+=x.eval_amt||0;tPnl+=x.pnl||0;tBuy+=x.buy_amt||0;});
 var tRt=tBuy>0?(tPnl/tBuy*100):0;
 box.innerHTML='<table class="tbl"><thead><tr><th>종목</th><th>전략</th>'+
  '<th style="text-align:right">보유수량</th><th style="text-align:right">매입단가</th>'+
  '<th style="text-align:right">현재가</th><th style="text-align:right">매입금액</th>'+
  '<th style="text-align:right">평가금액</th><th style="text-align:right">평가손익</th>'+
  '<th style="text-align:right">수익률</th></tr></thead><tbody>'+
  its.map(function(x){var up=(x.pnl||0)>=0;return '<tr><td><b>'+esc(x.ticker)+'</b>'+
   (x.name?'<br><span style="color:var(--c2);font-size:calc(10.5px*var(--ts-fs,1))">'+esc(x.name)+'</span>':'')+'</td>'+
   '<td>'+esc(x.display_name||'-')+'</td>'+
   '<td style="text-align:right">'+x.qty+'</td>'+
   '<td style="text-align:right">'+money(x.avg_price,2)+'</td>'+
   '<td style="text-align:right">'+money(x.now_price,2)+'</td>'+
   '<td style="text-align:right">'+money(x.buy_amt,2)+'</td>'+
   '<td style="text-align:right">'+money(x.eval_amt,2)+'</td>'+
   '<td style="text-align:right" class="'+(up?'up':'dn')+'">'+(up?'+':'')+money(x.pnl,2)+'</td>'+
   '<td style="text-align:right" class="'+(up?'up':'dn')+'">'+(up?'+':'')+Number(x.pnl_rt||0).toFixed(2)+'%</td></tr>';}).join('')+
  '</tbody></table>'+
  '<div style="padding:12px 18px"><div class="hl '+(tPnl>=0?'g':'r')+'" style="display:flex;gap:18px;'+
  'flex-wrap:wrap;font-size:calc(14px*var(--ts-fs,1))">합계 <span>매입 <b>'+money(tBuy)+'</b></span>'+
  '<span>평가 <b>'+money(tEval)+'</b></span><span>손익 <b class="'+(tPnl>=0?'up':'dn')+'">'+
  (tPnl>=0?'+':'')+money(tPnl)+'</b></span><span>수익률 <b class="'+(tRt>=0?'up':'dn')+'">'+
  (tRt>=0?'+':'')+tRt.toFixed(2)+'%</b></span></div></div>';}
function renderAlert(ss){var rows=[];ss.forEach(function(s){(s.errors||[]).forEach(function(l){
 rows.push({lv:l.level,m:'['+s.display_name+'] '+l.message,t:l.created_at});});});
 rows.sort(function(a,b){return (b.t||'').localeCompare(a.t||'');});
 $('palert').innerHTML=rows.length?rows.slice(0,7).map(function(r){
 var c=r.lv==='ERROR'?'e':(r.lv==='WARNING'?'w':'i');
 return '<div class="al"><span class="ad '+c+'"></span><span class="am">'+esc(r.m)+
 '<span class="at">'+esc((r.t||'').replace('T',' ').slice(0,19))+'</span></span></div>';}).join(''):
 '<div class="al"><span class="ad i"></span><span class="am">자동매매 정상 운영 중 · 최근 오류 없음</span></div>';}
function drawDonut(ss){if(C2){C2.destroy();C2=null;}var L=[],V=[],T=0;
 var isNH=(ACCT==='nh');
 ss.forEach(function(s){
  var v=isNH?(s.eval_amt||0):(s.realized_pnl||0);
  if(v){L.push(s.display_name);V.push(v);T+=v;}});
 $('dtot').innerHTML=(isNH?'':(T>=0?'+':''))+money(T);
 if(!L.length){$('lg2').innerHTML='<div class="muted">'+(isNH?'평가금 데이터 없음':'실현손익 데이터 없음')+'</div>';return;}
 C2=new Chart($('c2'),{type:'doughnut',data:{labels:L,datasets:[{data:V.map(Math.abs),
  backgroundColor:PAL,borderWidth:2,borderColor:'#fff'}]},options:{responsive:true,
  maintainAspectRatio:false,cutout:'66%',plugins:{legend:{display:false},
  tooltip:{callbacks:{label:function(c){return ' '+c.label+': '+money(V[c.dataIndex]);}}}}}});
 var tot=V.reduce(function(a,b){return a+Math.abs(b);},0)||1;
 $('lg2').innerHTML=L.map(function(n,i){return '<div class="r"><i style="background:'+PAL[i%5]+
  '"></i><span class="n">'+esc(n)+'</span><span class="a '+(V[i]>=0?'up':'dn')+'">'+
  (V[i]>=0?'+':'')+money(V[i])+'</span><span class="p">'+
  (Math.abs(V[i])/tot*100).toFixed(1)+'%</span></div>';}).join('');}
function dDays(r){return {'1주':7,'1개월':30,'3M':90,'6M':180,'전체':99999}[r]||90;}
/* 대시보드 NH 탭 그래프 — VR 메뉴의 기수 그래프와 같은 API(/vr/api/gisu/{gid}/graph)·같은 계열.
   주차별 평가금(실선) + 밴드 최소/최대(점선). 값·계산은 VR 화면과 동일하고 여기서 만들지 않는다. */
/* 토스 계좌 자산 추이 — 10분마다 기록한 스냅샷(평가+예수금)을 그대로 그린다.
   하루치가 2일 이상 쌓이면 일자별(그날 마지막 값), 그 전에는 기록 시각 그대로. */
var TOSSSER=null;
function drawTossLine(){var w=$('cw1');if(!w)return;
 var t=MET.toss||{};
 var info=function(title,sub){w.innerHTML='<div class="empty"><i class="fa-solid fa-building-columns"></i>'+
  '<div class="t">'+title+'</div><div class="s">'+sub+'</div>'+
  '<div style="margin-top:10px"><button class="btn sm" onclick="tossRefresh()">지금 갱신</button></div></div>';};
 var render=function(ser){
  var pts=(ser&&ser.points)||[];
  if(pts.length<2){
   info(t.error?'토스 조회 오류':'토스 자산 추이 수집중',
    t.error?esc(String(t.error).slice(0,120))
    :('10분마다 잔고를 기록합니다 (현재 '+pts.length+'개). 점이 2개 이상 쌓이면 그래프가 표시됩니다.<br>'+
      '평가 '+money(t.eval_total)+' · 예수금 '+money(t.cash_usd)+
      (t.cash_krw?(' + '+Math.round(t.cash_krw).toLocaleString()+'원'+(t.fx?(' (≈'+money(t.cash_krw/t.fx)+')'):'')):'')+
      (t.ts?(' · 갱신 '+esc(t.ts)):'')));
   return;}
  /* 일자별 마지막 점으로 묶기 — 2일 이상이면 일자 라벨, 아니면 시각 라벨 */
  var bym={},order=[];
  pts.forEach(function(p){var d=String(p.ts||'').slice(0,10);if(!(d in bym))order.push(d);bym[d]=p;});
  var daily=order.length>=2, use=daily?order.map(function(d){return bym[d];}):pts;
  var L=use.map(function(p){return daily?String(p.ts).slice(5,10):String(p.ts).slice(11,16);});
  var tot=use.map(function(p){return p.total_assets;});
  var ev=use.map(function(p){return p.eval_usd;});
  if(C1){C1.destroy();C1=null;}
  w.innerHTML='<canvas id="c1"></canvas>';
  var g=$('c1').getContext('2d').createLinearGradient(0,0,0,264);
  g.addColorStop(0,'rgba(47,107,255,.22)');g.addColorStop(.65,'rgba(47,107,255,.05)');
  g.addColorStop(1,'rgba(47,107,255,0)');
  C1=new Chart($('c1'),{type:'line',data:{labels:L,datasets:[
   {label:'총자산',data:tot,borderColor:'#2f6bff',backgroundColor:g,borderWidth:2.6,
    pointRadius:0,pointHoverRadius:4,fill:true,tension:.3},
   {label:'주식 평가금',data:ev,borderColor:'#16a34a',borderDash:[6,4],borderWidth:1.8,
    pointRadius:0,pointHoverRadius:4,tension:.3}]},
   options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
    plugins:{legend:{position:'bottom',labels:{usePointStyle:true,boxWidth:7,font:{size:11*TSFS}}},
     tooltip:{callbacks:{label:function(c){return c.dataset.label+' '+money(c.parsed.y);}}}},
    scales:{x:{grid:{display:false},ticks:{color:'#9aa3b2',font:{size:10*TSFS},maxTicksLimit:8}},
     y:{grid:{color:'#eef1f6'},ticks:{color:'#9aa3b2',font:{size:10*TSFS},
      callback:function(v){return '$'+(v/1000).toFixed(1)+'k';}}}}}});};
 if(TOSSSER){render(TOSSSER);return;}
 info('토스 자산 추이 불러오는 중','');
 fetch('/api/suite/toss/series').then(function(r){return r.json();})
  .then(function(d){TOSSSER=d;if(ACCT==='toss')render(d);})
  .catch(function(){info('토스 자산 추이 로드 실패','잠시 후 다시 시도하세요');});}
function drawVrDash(){var w=$('cw1');if(!w)return;
 var list=((MET&&MET.nh&&MET.nh.accounts)||[]);
 var msg=function(ic,t,s){w.innerHTML='<div class="empty"><i class="fa-solid '+ic+'"></i>'+
  '<div class="t">'+t+'</div><div class="s">'+s+'</div></div>';};
 if(!list.length){msg('fa-scale-balanced','VR 기수 없음','NH 계좌 기수가 등록되면 주차별 추이가 표시됩니다');return;}
 var gid=VRSEL||list[0].gisu;
 fetch('/vr/api/gisu/'+gid+'/graph').then(function(r){if(!r.ok)throw 0;return r.json();}).then(function(gd){
  if(ACCT!=='nh')return;                       /* 응답 도착 전 계좌를 바꿨으면 그리지 않는다 */
  var rows=(gd&&gd.weekly)||[];
  if(rows.length<2){msg('fa-chart-line','주차 기록 누적 중','2주차 이상 기록되면 라오어식 추이가 표시됩니다');return;}
  var ev=rows.map(function(r){return r.eval_amt;});
  if(gd.live_eval!=null){var idx=rows.findIndex(function(r){return r.week_no===gd.current_week;});
   if(idx>=0&&ev[idx]==null)ev[idx]=gd.live_eval;}
  if(C1){C1.destroy();C1=null;}
  w.innerHTML='<canvas id="c1"></canvas>';
  C1=new Chart($('c1'),{type:'line',data:{labels:rows.map(function(r){return r.week_no+'주';}),datasets:[
   {label:'평가금',data:ev,borderColor:'#e5484d',backgroundColor:'rgba(229,72,77,.08)',
    borderWidth:2.4,pointRadius:3,pointHoverRadius:5,tension:.15,spanGaps:true,fill:true},
   {label:'최소',data:rows.map(function(r){return r.band_lo;}),borderColor:'#6c5ce7',
    borderDash:[6,4],borderWidth:1.6,pointRadius:0,spanGaps:true},
   {label:'최대',data:rows.map(function(r){return r.band_hi;}),borderColor:'#6c5ce7',
    borderDash:[6,4],borderWidth:1.6,pointRadius:0,spanGaps:true}]},
   options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
    plugins:{legend:{position:'bottom',labels:{usePointStyle:true,boxWidth:7,font:{size:11*TSFS}}},
     tooltip:{callbacks:{label:function(c){return c.dataset.label+' '+money(c.parsed.y);}}}},
    scales:{x:{grid:{display:false},ticks:{color:'#9aa3b2',font:{size:10*TSFS}}},
     y:{grid:{color:'#eef1f6'},ticks:{color:'#9aa3b2',font:{size:10*TSFS},
      callback:function(v){return '$'+(v/1000).toFixed(0)+'k';}}}}}});
 }).catch(function(){msg('fa-triangle-exclamation','VR 추이 로드 실패','VR (NH계좌) 메뉴에서 확인하세요');});}
function drawLine(){var w=$('cw1');if(!w)return;
 if(ACCT==='toss'){drawTossLine();return;}
 if(ACCT==='nh'){drawVrDash();return;}
 if(!SER||SER.collecting||!SER.points||SER.points.length<2){
  w.innerHTML='<div class="empty"><i class="fa-solid fa-chart-area"></i>'+
  '<div class="t">자산추이 데이터 수집중</div><div class="s">equity 스냅샷 30분 주기 누적 시 표시</div></div>';return;}
 if(C1){C1.destroy();C1=null;}w.innerHTML='<canvas id="c1"></canvas>';
 var n=dDays(R1),p=SER.points,last=new Date(p[p.length-1].ts),cut=new Date(last-n*864e5);
 var f=p.map(function(x,i){return {x:x,i:i};}).filter(function(o){return new Date(o.x.ts)>=cut;});
 // 일(日) 단위 집계: 날짜별 마지막 스냅샷 1포인트 = 그날의 자산/수익률
 var bym={},order=[];
 f.forEach(function(o){var d=String(o.x.ts).slice(0,10);if(!(d in bym))order.push(d);bym[d]=o;});
 var dp=order.map(function(d){return bym[d];});
 if(dp.length<2){w.innerHTML='<div class="empty"><i class="fa-solid fa-calendar-day"></i>'+
  '<div class="t">일별 추이 누적 중</div><div class="s">거래일이 2일 이상 쌓이면 일자별 추이가 표시됩니다 (현재 '+
  dp.length+'일치)</div></div>';return;}
 var L=dp.map(function(o){return String(o.x.ts).slice(5,10);});
 var vals=dp.map(function(o){return o.x.total_assets;});
 // ── 세로축 다이내믹 레인지: 데이터 min~max에 15% 패딩만 → 변화가 드라마틱하게 보이도록
 var vmin=Math.min.apply(null,vals),vmax=Math.max.apply(null,vals);
 var span=Math.max(vmax-vmin,vmax*0.01,1),pad=span*0.15;
 var yMin=Math.max(0,Math.floor((vmin-pad)/50)*50),yMax=Math.ceil((vmax+pad)/50)*50;
 // ── 입금/출금: 누적선 → 발생일 이벤트 막대(복합차트, 우측 보조축)
 var depB=[],wdrB=[],maxBar=0;
 dp.forEach(function(o,i){var pv=i>0?dp[i-1].x:null;
  var d=pv?Math.max(0,(o.x.deposit||0)-(pv.deposit||0)):0;
  var wd=pv?Math.max(0,(o.x.withdraw||0)-(pv.withdraw||0)):0;
  depB.push(d>0?d:null);wdrB.push(wd>0?wd:null);
  if(d>maxBar)maxBar=d;if(wd>maxBar)maxBar=wd;});
 // ── 총자산 라인: 그라데이션 필 + 부드러운 곡선
 var gctx=$('c1').getContext('2d');
 var grad=gctx.createLinearGradient(0,0,0,264);
 grad.addColorStop(0,'rgba(47,107,255,.22)');grad.addColorStop(.65,'rgba(47,107,255,.05)');
 grad.addColorStop(1,'rgba(47,107,255,0)');
 var ds=[{label:'총자산',type:'line',data:vals,borderColor:'#2f6bff',
  backgroundColor:grad,borderWidth:2.6,pointRadius:0,pointHoverRadius:4,
  pointHoverBackgroundColor:'#2f6bff',fill:true,tension:.3,yAxisID:'y',order:1}];
 if(maxBar>0){
  ds.push({label:'입금',type:'bar',data:depB,backgroundColor:'rgba(22,163,74,.55)',
   borderColor:'#16a34a',borderWidth:1,borderRadius:5,maxBarThickness:16,yAxisID:'y2',order:2});
  ds.push({label:'출금',type:'bar',data:wdrB,backgroundColor:'rgba(229,72,77,.55)',
   borderColor:'#e5484d',borderWidth:1,borderRadius:5,maxBarThickness:16,yAxisID:'y2',order:2});}
 C1=new Chart($('c1'),{type:'line',data:{labels:L,datasets:ds},options:{responsive:true,
  maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
  plugins:{legend:{position:'bottom',labels:{usePointStyle:true,pointStyle:'circle',boxWidth:7,
   padding:14,font:{size:11*TSFS}}},tooltip:{backgroundColor:'#1a2233',padding:11,cornerRadius:9,
   callbacks:{label:function(c){return ' '+c.dataset.label+': $'+
    Number(c.parsed.y).toLocaleString(undefined,{maximumFractionDigits:0});}}}},
  scales:{x:{grid:{display:false},title:{display:true,text:'일자',color:'#9aa3b2',font:{size:10*TSFS}},
   ticks:{color:'#9aa3b2',font:{size:10*TSFS},maxTicksLimit:10}},
  y:{position:'left',min:yMin,max:yMax,grid:{color:'#eef1f6'},
   ticks:{color:'#9aa3b2',font:{size:10*TSFS},maxTicksLimit:7,
   callback:function(v){return '$'+(v>=1000?(v/1000).toFixed(1)+'k':v);}}},
  y2:{display:false,min:0,max:Math.max(maxBar*3.2,1)}}}});}
/* ---------- 전략 관리 ---------- */
function pgStrat(){var ss=MET.strategies||[];
 var opt=STRATS.map(function(s){return '<option value="'+s.key+'">'+esc(s.label)+'</option>';}).join('');
 var h='<div class="grid g-2"><div class="card">'+
  '<div class="ch"><span class="ct"><i class="fa-solid fa-list-check"></i>전략 · 시드</span></div>'+
  '<div class="form"><div class="fld"><label>전략</label><select id="sSel">'+opt+'</select></div>'+
  '<div class="fld"><label>전략 시드 할당 총액 (USD)</label><input id="sBud" type="number" placeholder="예: 10000"></div>'+
  '<div class="fnote" id="sBinfo">—</div>'+
  '<div class="fld" style="grid-column:1/-1"><label>원금 운용 방식</label>'+
  '<select id="sCmp" onchange="saveCompound()">'+
  '<option value="simple">단리 — 원금 고정</option>'+
  '<option value="compound">복리 — 실현손익만큼 원금 증액</option></select></div>'+
  '<div class="fnote" id="sCmpInfo" style="grid-column:1/-1">—</div>'+
  '<div class="tip" style="grid-column:1/-1;margin:0" id="sLogic">'+
  '<i class="fa-solid fa-circle-info"></i><span>전략 로직</span></div>'+
  '<div class="fact"><button class="btn p" onclick="saveBudget()">시드 할당 저장</button></div></div></div>'+
  '<div class="card"><div class="ch"><span class="ct"><i class="fa-solid fa-plus"></i>티커 추가</span></div>'+
  '<div id="addForm"></div></div></div>';
 h+='<div class="grid">'+card('종목 목록','fa-coins','<div id="tklist" style="overflow-x:auto"></div>',
  '<span class="lk" onclick="loadStratMgr()">새로고침</span>')+'</div>';
 $('page').innerHTML=h;
 $('sSel').onchange=loadStratMgr;loadStratMgr();}
function loadStratMgr(){var k=$('sSel').value;var bud=(MET&&MET.strategies||[]).filter(function(s){
  return s.strategy===k;})[0];
 var sm=STRATS.filter(function(s){return s.key===k;})[0]||{};
 $('sLogic').innerHTML='<i class="fa-solid fa-circle-info"></i><span><b>전략 로직 — '+
  esc(sm.label||k)+'</b><br>'+esc(sm.logic||'설명 없음')+'</span>';
 fetch('/api/suite/strategies').then(function(r){return r.json();}).then(function(d){
  var b=(d.budgets||[]).filter(function(x){return x.strategy===k;})[0]||{};
  SMBUD=b;
  $('sBud').value=b.assigned_total!=null?b.assigned_total:'';
  $('sBinfo').innerHTML='현재 사용 <b>'+money(b.used)+'</b> / 할당 <b>'+
   (b.assigned_total==null?'미설정':money(b.assigned_total))+'</b> · 종목 '+(b.ticker_count||0)+
   (b.over_budget?' · <span class="dn">예산 초과</span>':'');});
 loadCompound(k);
 var kind=kindOf(k);
 var jong=(kind==='jongsa');
 // 트렌치형(떨사오팔/종사종팔): x(%) 라벨·기본값·안내문만 전략별로 다름. API/필드ID는 동일(Ticker API).
 var xLabel=jong?'목표 수익률 (%)':'x (%)';
 var xDefault=jong?'3.5':'3';
 var ntDefault=jong?'7':'5';   // 종사종팔 기본 7트렌치 (사용자 변경 가능)
 var trNote=jong
  ?'종사종팔: 매 거래일 다음 트렌치를 <b>종가 LOC 매수(한도 전일종가+15%)</b>, <b>전체평단 +목표% 도달 시 전량 일괄매도</b>, 40거래일 손절. 티커는 전 전략 통틀어 중복 불가.'
  :'떨사오팔: 총액을 트렌치로 분할(전일종가 −x% LOC 매수). 티커는 전 전략 통틀어 중복 불가.';
 $('addForm').innerHTML=kind==='infinite'?
  ('<div class="form"><div class="fld"><label>티커</label><input id="fTk" placeholder="예: SOXL"></div>'+
   '<div class="fld"><label>시드 (USD)</label><input id="fSeed" type="number" placeholder="예: 5000"></div>'+
   '<div class="fld"><label>분할수 A</label><input id="fA" type="number" value="40"></div>'+
   '<div class="fld"><label>목표수익률 R (%)</label><input id="fR" type="number" value="10"></div>'+
   '<div class="fnote">무한매수법: 시드를 A회 분할 매수. 티커는 전 전략 통틀어 중복 불가.</div>'+
   '<div class="fact"><button class="btn p" onclick="addTicker()">티커 추가</button></div></div>'):
  ('<div class="form"><div class="fld"><label>티커</label><input id="fTk" placeholder="'+(jong?'예: QQQ':'예: TECL')+'"></div>'+
   '<div class="fld"><label>총 투입금액 (USD)</label><input id="fSeed" type="number" placeholder="예: 5000"></div>'+
   '<div class="fld"><label>트렌치 수</label><input id="fNt" type="number" value="'+ntDefault+'"></div>'+
   '<div class="fld"><label>'+xLabel+'</label><input id="fX" type="number" step="0.1" value="'+xDefault+'"></div>'+
   '<div class="fld"><label>손절 거래일</label><input id="fLc" type="number" value="40"></div>'+
   '<div class="fnote">'+trNote+'</div>'+
   '<div class="fact"><button class="btn p" onclick="addTicker()">티커 추가</button></div></div>');
 var lp=kind==='infinite'?('/'+k+'/api/portfolios'):('/'+k+'/api/tickers');
 fetch(lp).then(function(r){return r.json();}).then(function(rows){
  if(!rows||!rows.length){$('tklist').innerHTML='<div class="muted">등록된 종목 없음</div>';return;}
  var inf=kind==='infinite';
  SMROWS={};rows.forEach(function(r){SMROWS[r.id]=r;});
  $('tklist').innerHTML='<table class="tbl"><thead><tr><th>티커</th>'+
   '<th style="text-align:right">'+(inf?'시드':'총액')+'</th>'+
   '<th style="text-align:right">'+(inf?'분할(A)':'트렌치')+'</th>'+
   (inf?'<th style="text-align:right">T(회차)</th><th style="text-align:right">☆%</th>'+
    '<th>모드</th><th style="text-align:right">싸이클</th>':'')+
   '<th>진행</th><th></th></tr></thead><tbody>'+rows.map(function(r){
   var amt=inf?r.seed:r.total_usd;var div=inf?r.A:r.num_tranches;var on=r.trading_enabled;
   var infc=inf?('<td style="text-align:right"><b>'+(r.T==null?'—':Number(r.T).toFixed(1))+
    '</b></td><td style="text-align:right">'+(r.star_pct==null?'—':Number(r.star_pct).toFixed(2)+'%')+
    '</td><td><span class="bdg '+((''+r.mode).indexOf('QUARTER')>=0?'part':'run')+'">'+
    esc(r.mode||'NORMAL')+'</span></td><td style="text-align:right">C'+(r.current_cycle||1)+'</td>'):'';
   return '<tr><td><b>'+esc(r.ticker)+'</b></td><td style="text-align:right">'+money(amt)+
   '</td><td style="text-align:right">'+div+'</td>'+infc+'<td><span class="bdg '+(on?'run':'stop')+'">'+
   (on?'진행':'대기')+'</span></td><td style="text-align:right">'+
   '<button class="btn sm" onclick="editSeed(\''+k+'\','+r.id+')">시드 수정</button> '+
   '<button class="btn sm" onclick="togTrade(\''+k+'\','+r.id+')">진행 토글</button> '+
   '<button class="btn sm dg" onclick="delTicker(\''+k+'\','+r.id+')">삭제</button></td></tr>';
  }).join('')+'</tbody></table>';});}
function loadCompound(k){var el=$('sCmp'),info=$('sCmpInfo');if(!el)return;
 fetch('/api/suite/compound').then(function(r){return r.json();}).then(function(d){
  var it=(d.items||[]).filter(function(x){return x.strategy===k;})[0];
  if(!it){info.textContent='';return;}
  el.value=it.mode;
  if(it.mode==='compound'){
   info.innerHTML='<span class="hl g" style="display:inline-block;padding:8px 12px">복리 운용중 · '+
    '기준원금 <b>'+money(it.base_seed)+'</b> + 증액 <b>'+money(it.added)+'</b> = 현재 <b>'+money(it.current_seed)+'</b>'+
    (it.gain>it.added?(' <span style="color:var(--c2)">(다음 싸이클 종료 시 '+money(it.target_seed)+' 예정)</span>'):'')+
    (it.cap_note?('<br><span class="dn">'+esc(it.cap_note)+'</span>'):'')+'</span>';
  }else{
   info.innerHTML='단리 운용중 — 실현손익은 원금에 반영되지 않습니다. '+
    '<span style="color:var(--c2)">복리 전환 시 이후 발생하는 실현손익만 가산(소급 없음), 손실 시 감액 없음.</span>';
  }}).catch(function(){info.textContent='';});}
function saveCompound(){var k=$('sSel').value,m=$('sCmp').value;
 var msg=(m==='compound')
  ?'복리 모드로 전환합니다.\n\n· 지금부터 발생하는 실현손익만큼 원금이 늘어납니다(과거분 소급 없음)\n· 증액은 싸이클 종료 시점에만, 다음 싸이클부터 적용\n· 손실이어도 원금은 줄지 않습니다\n진행할까요?'
  :'단리 모드로 되돌립니다.\n\n원금이 [전략 시드 할당 총액] 기준으로 복원됩니다. 진행할까요?';
 if(!confirm(msg)){loadCompound(k);return;}
 api('POST','/api/suite/compound/'+k,{mode:m}).then(function(r){
  toast(m==='compound'?'복리 모드 적용됨':'단리 모드로 복원됨');
  loadStratMgr();}).catch(function(e){toast('실패: '+e);loadCompound(k);});}
function saveBudget(){var k=$('sSel').value;var v=parseFloat($('sBud').value);
 if(isNaN(v)){toast('할당액을 입력하세요');return;}
 api('POST','/api/suite/strategies/'+k+'/budget',{total_usd:v}).then(function(){
  toast('시드 할당 저장됨');loadStratMgr();}).catch(function(e){toast('실패: '+e);});}
function addTicker(){var k=$('sSel').value,kind=kindOf(k);var tk=($('fTk').value||'').trim().toUpperCase();
 if(!tk){toast('티커를 입력하세요');return;}
 var url,body;
 if(kind==='infinite'){url='/'+k+'/api/portfolios';body={ticker:tk,seed:parseFloat($('fSeed').value),
  A:parseInt($('fA').value)||40,R:parseFloat($('fR').value)||10};}
 else{url='/'+k+'/api/tickers';body={ticker:tk,total_usd:parseFloat($('fSeed').value),
  num_tranches:parseInt($('fNt').value)||5,x_pct:parseFloat($('fX').value)||3,
  loss_cut_days:parseInt($('fLc').value)||40};}
 if(isNaN(body.seed)&&isNaN(body.total_usd)){toast('금액을 입력하세요');return;}
 api('POST',url,body).then(function(r){toast(r.message||'티커 추가됨');loadStratMgr();})
  .catch(function(e){toast('실패: '+e);});}
/* 종목 시드(무한=seed, 트렌치형=총액) 변경 — 미리보기(변경 전/후) 확인 후 저장. 싸이클 중에도 다음 주문부터 반영 */
var SMROWS={},SMBUD={};
function editSeed(k,id){var inf=kindOf(k)==='infinite';var r=SMROWS[id];if(!r)return;
 var cur=inf?r.seed:r.total_usd;
 var v=prompt(r.ticker+' 시드 변경 (USD)\n현재 '+money(cur),cur);
 if(v==null)return;v=parseFloat(String(v).replace(/[,$\s]/g,''));
 if(!(v>0)){toast('금액을 확인하세요');return;}
 if(Math.abs(v-cur)<0.005){toast('변경 없음');return;}
 var url='/'+k+'/api/'+(inf?('portfolios/'+id):('tickers/'+id+'/seed'));
 var pv=inf?{seed:v,preview:true}:{total_usd:v,preview:true};
 api('PATCH',url,pv).then(function(p){var b=p.before,a=p.after,msg;
  if(inf){msg=p.ticker+' 시드 '+money(b.seed)+' → '+money(a.seed)+'\n\n'+
   '1회 매수액   '+money(b.B)+' → '+money(a.B)+'\n'+
   'T   '+b.T+' ('+b.half+') → '+a.T+' ('+a.half+')\n'+
   '☆%   '+b.star_pct+'% → '+a.star_pct+'%\n'+
   '남은 매수 여력   '+money(b.remaining)+' → '+money(a.remaining)+'  (보유 매입 '+money(p.cost)+')';}
  else{msg=p.ticker+' 총액 '+money(b.total_usd)+' → '+money(a.total_usd)+'\n\n'+
   '트렌치 1회 매수액   '+money(b.per_tranche)+' → '+money(a.per_tranche)+'\n'+
   '진행   '+p.bought+'/'+p.num_tranches+' 트렌치 매수됨 (매입 '+money(p.cost)+')\n'+
   (p.idle>0?('→ 남은 '+p.idle+'개 트렌치부터 새 금액으로 매수 (이미 산 트렌치는 그대로)')
    :'→ 트렌치가 다 차 있어 다음 싸이클부터 적용')+
   (p.seed_reflect?'\n※ 추가입금 반영(ON) 상태 — 이번 싸이클 매수액은 예수금 기준으로 계산됩니다':'');}
  var bu=SMBUD||{};
  if(bu.assigned_total!=null){var nu=(bu.used||0)-cur+v;
   if(nu>bu.assigned_total+0.005)msg+='\n\n※ 전략 시드 할당 총액 '+money(bu.assigned_total)+'을 넘습니다 (합계 '+
    money(nu)+') — 위 [시드 할당]도 올려 두세요';}
  msg+='\n\n다음 주문부터 반영됩니다 (이미 접수된 주문은 그대로). 저장할까요?';
  if(!confirm(msg))return;
  return api('PATCH',url,inf?{seed:v}:{total_usd:v}).then(function(x){
   toast(x.message||'시드 변경됨');loadStratMgr();});
 }).catch(function(e){toast('실패: '+e);});}
function togTrade(k,id){var kind=kindOf(k);
 api('PATCH','/'+k+'/api/'+(kind==='infinite'?'portfolios':'tickers')+'/'+id+'/trading')
  .then(function(){toast('진행 상태 변경');loadStratMgr();}).catch(function(e){toast('실패: '+e);});}
function delTicker(k,id){if(!confirm('이 종목을 삭제할까요? (성공리포트는 보존)'))return;
 var kind=kindOf(k);
 api('DELETE','/'+k+'/api/'+(kind==='infinite'?'portfolios':'tickers')+'/'+id)
  .then(function(){toast('삭제됨');loadStratMgr();}).catch(function(e){toast('실패: '+e);});}
/* ---------- 포트폴리오 ---------- */
function pgPort(){var ss=(MET.strategies||[]).concat(((MET.toss||{}).strategies)||[]);var rows=[];
 ss.forEach(function(s){(s.holdings||[]).forEach(function(h){rows.push({d:s.display_name,
  t:h.ticker,q:h.qty,a:h.avg_price,c:h.cost,kill:s.kill_switch});});});
 var tc=rows.reduce(function(a,b){return a+b.c;},0);
 var h='<div class="kpis">'+
  kpi('보유 종목','fa-briefcase','b',rows.length+' 종목','','전 전략 합산')+
  kpi('매입원가 합계','fa-coins','n',money(tc),'','평단×수량 기준')+
  kpi('전략 수','fa-layer-group','n',ss.length+' 개','보유 기준')+'</div>';
 h+='<div class="grid">'+card('보유종목 현황','fa-briefcase',rows.length?
  ('<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>종목</th><th>전략</th>'+
  '<th style="text-align:right">보유수량</th><th style="text-align:right">평단가</th>'+
  '<th style="text-align:right">매입금액</th><th>상태</th></tr></thead><tbody>'+
  rows.map(function(r){return '<tr><td><b>'+esc(r.t)+'</b></td><td>'+esc(r.d)+
  '</td><td style="text-align:right">'+r.q+'</td><td style="text-align:right">'+money(r.a,2)+
  '</td><td style="text-align:right">'+money(r.c)+'</td><td><span class="bdg '+
  (r.kill?'stop':'run')+'">'+(r.kill?'정지':'운용중')+'</span></td></tr>';}).join('')+
  '</tbody></table></div>'):'<div class="muted">보유 종목 없음</div>')+'</div>';
 h+='<div class="tip"><i class="fa-solid fa-circle-info"></i>단일 공용계좌라 종목별 실시간 '+
  '평가손익은 KIS 추가호출 없이 산출하지 않습니다. 계좌 단위 평가손익은 대시보드 KPI를 참고하세요.</div>';
 var dS=STRATS.filter(function(s){return s.kind!=='infinite';});
 if(dS.length){h+='<div class="grid">'+card('가상 트렌치 현황 · 떨사오팔 / 종사종팔','fa-layer-group',
  '<div id="vtr"><div class="muted">트렌치 불러오는 중…</div></div>',
  '<span style="font-size:calc(11.5px*var(--ts-fs,1));color:var(--c2)">매수=초록 · 대기=회색</span>')+'</div>';}
 $('page').innerHTML=h;
 if(dS.length)loadTranches(dS);}
function loadTranches(dS){var box=$('vtr');var blocks=[];var pend=0;
 function done(){if(pend<=0)box.innerHTML=blocks.length?blocks.join(''):
  '<div class="muted">활성 트렌치 종목 없음</div>';}
 dS.forEach(function(s){pend++;
  fetch('/'+s.key+'/api/tickers').then(function(r){return r.json();}).then(function(tks){
   var act=(tks||[]).filter(function(t){return t.is_active;});
   if(!act.length){pend--;done();return;}
   var c=0;act.forEach(function(tk){
    fetch('/'+s.key+'/api/tickers/'+tk.id+'/tranches').then(function(r){return r.json();})
    .then(function(d){blocks.push(trBlock(s.label,d,tk));}).catch(function(){})
    .then(function(){c++;if(c===act.length){pend--;done();}});});
  }).catch(function(){pend--;done();});});}
function trBlock(strat,d,tk){var trs=(d&&d.tranches)||[];
 var bg=trs.filter(function(t){return t.status==='BOUGHT';}).length;
 var cells=trs.map(function(t){var on=t.status==='BOUGHT';
  return '<span class="trc'+(on?' on':'')+'" title="T'+t.tranche_num+
  (on?(' 평단 '+money(t.avg_price,2)+' · '+t.qty+'주'):' 대기')+'">'+t.tranche_num+'</span>';}).join('');
 var det=trs.map(function(t){var on=t.status==='BOUGHT';
  return '<tr><td>T'+t.tranche_num+'</td><td><span class="bdg '+(on?'run">매수':'stop">대기')+
  '</span></td><td style="text-align:right">'+(on?money(t.avg_price,2):'—')+
  '</td><td style="text-align:right">'+(on?t.qty:0)+'</td><td>'+esc(t.buy_date||'—')+
  '</td><td style="text-align:right">'+(t.days_held||0)+'일</td><td style="text-align:right">'+
  money(t.amount_per_tranche)+'</td></tr>';}).join('');
 return '<div class="vtblk"><div class="vth"><b>'+esc((d&&d.ticker)||tk.ticker)+'</b>'+
  '<span style="color:var(--c2);font-size:calc(11.5px*var(--ts-fs,1))">'+esc(strat)+'</span>'+
  '<span class="bdg run" style="margin-left:auto">'+bg+' / '+trs.length+' 매수</span></div>'+
  '<div class="trcells">'+cells+'</div>'+
  '<details><summary>트렌치 상세 보기</summary><div style="overflow-x:auto"><table class="tbl">'+
  '<thead><tr><th>트렌치</th><th>상태</th><th style="text-align:right">평단</th>'+
  '<th style="text-align:right">수량</th><th>매수일</th><th style="text-align:right">보유일</th>'+
  '<th style="text-align:right">트렌치금액</th></tr></thead><tbody>'+
  (det||'<tr><td colspan=7 style="text-align:center;color:#9aa3b2">트렌치 없음</td></tr>')+
  '</tbody></table></div></details></div>';}
/* ---------- 주문/체결 ---------- */
var TRD=[],TRDF={s:'',t:'',d1:'',d2:''},TRDS={k:'trade_date',d:-1};
function trdMatch(o){
 if(TRDF.s && o._sk!==TRDF.s) return false;
 if(TRDF.t && (o.ticker||'').toUpperCase().indexOf(TRDF.t.toUpperCase().trim())<0) return false;
 var d=String(o.trade_date||'');
 if(TRDF.d1 && d<TRDF.d1) return false;
 if(TRDF.d2 && d>TRDF.d2) return false;
 return true;}
function trdSort(rows){var k=TRDS.k,d=TRDS.d;
 var numeric={qty:1,price:1,amount:1,cycle_number:1,tranche_num:1};
 return rows.slice().sort(function(a,b){var va=a[k],vb=b[k];
  if(numeric[k]){va=(va==null?-1:+va);vb=(vb==null?-1:+vb);
   if(isNaN(va))va=-1; if(isNaN(vb))vb=-1;}
  else{va=(va==null?'':va)+'';vb=(vb==null?'':vb)+'';}
  return va<vb?-d:(va>vb?d:0);});}
function trdSetSort(k){if(TRDS.k===k)TRDS.d=-TRDS.d;
 else{TRDS.k=k;TRDS.d=(k==='trade_date'||k==='qty'||k==='price'||k==='amount'||
   k==='cycle_number'||k==='tranche_num')?-1:1;}
 renderTradesTable();}
function trdReset(){TRDF={s:'',t:'',d1:'',d2:''};TRDS={k:'trade_date',d:-1};renderTradesTable();}
function _ymd2iso(s){s=String(s||'');return s.length===8?s.substr(0,4)+'-'+s.substr(4,2)+'-'+s.substr(6,2):'';}
function renderTradesTable(){var rows=trdSort(TRD.filter(trdMatch));
 var arrow=function(k){return TRDS.k===k?(TRDS.d>0?' ▲':' ▼'):' ↕';};
 var H=function(k,label,right){return '<th style="cursor:pointer;user-select:none'+
   (right?';text-align:right':'')+'" onclick="trdSetSort(\''+k+'\')">'+label+
   '<span style="color:var(--c3);font-size:calc(10px*var(--ts-fs,1))">'+arrow(k)+'</span></th>';};
 var stratOpts='<option value="">전략 전체</option>'+STRATS.map(function(s){
   return '<option value="'+s.key+'"'+(TRDF.s===s.key?' selected':'')+'>'+esc(s.label)+'</option>';}).join('');
 var ipStyle='padding:7px 10px;border:1px solid var(--line);border-radius:8px;'+
   'font-family:inherit;font-size:calc(12.5px*var(--ts-fs,1));background:#fff;color:var(--c0)';
 var bar='<div style="padding:12px 16px;border-bottom:1px solid var(--line);'+
   'display:flex;flex-wrap:wrap;gap:8px;align-items:center">'+
   '<select onchange="TRDF.s=this.value;renderTradesTable();" style="'+ipStyle+'">'+stratOpts+'</select>'+
   '<input placeholder="티커" value="'+esc(TRDF.t||'')+'" '+
   'oninput="TRDF.t=this.value;renderTradesTable();" style="'+ipStyle+';width:100px;text-transform:uppercase">'+
   '<input type="date" value="'+_ymd2iso(TRDF.d1)+'" '+
   'onchange="TRDF.d1=this.value.replace(/-/g,\'\');renderTradesTable();" style="'+ipStyle+'">'+
   '<span style="color:var(--c2);font-size:calc(11.5px*var(--ts-fs,1))">~</span>'+
   '<input type="date" value="'+_ymd2iso(TRDF.d2)+'" '+
   'onchange="TRDF.d2=this.value.replace(/-/g,\'\');renderTradesTable();" style="'+ipStyle+'">'+
   '<button class="btn sm" onclick="trdReset();"><i class="fa-solid fa-rotate-left"></i> 초기화</button>'+
   '<span style="color:var(--c2);font-size:calc(11.5px*var(--ts-fs,1));margin-left:auto">'+
   rows.length+' / '+TRD.length+'건</span></div>';
 var tbl=rows.length?('<div style="overflow-x:auto"><table class="tbl"><thead><tr>'+
   H('trade_date','일자')+H('_s','전략')+H('ticker','티커')+
   H('cycle_number','싸이클',1)+H('tranche_num','회차',1)+H('side','구분')+
   H('qty','수량',1)+H('price','체결가',1)+H('amount','금액',1)+'</tr></thead><tbody>'+
   rows.map(function(o){var cy=(o.cycle_number!=null?'C'+o.cycle_number:'—');
   var tr=(o.tranche_num!=null?'T'+o.tranche_num:(o.buy_seq!=null?'T'+o.buy_seq:'—'));
   return '<tr><td>'+esc(o.trade_date)+'</td><td>'+esc(o._s)+
   '</td><td><b>'+esc(o.ticker)+'</b></td>'+
   '<td style="text-align:right;color:var(--c1)"><b>'+cy+'</b></td>'+
   '<td style="text-align:right;color:var(--c1)">'+tr+'</td>'+
   '<td><span class="tag '+(o.side==='buy'?'buy">매수':'sell">매도')+'</span></td>'+
   '<td style="text-align:right">'+o.qty+'</td>'+
   '<td style="text-align:right">'+money(o.price,2)+'</td>'+
   '<td style="text-align:right">'+money(o.amount,2)+'</td></tr>';}).join('')+
   '</tbody></table></div>'):
   '<div class="muted">조건에 맞는 체결 내역 없음</div>';
 $('otab').innerHTML=bar+tbl;}
function pgOrder(){$('page').innerHTML='<div class="grid">'+
 '<div class="card"><div class="ch"><span class="ct"><i class="fa-solid fa-receipt"></i>주문 · 체결</span>'+
 '<div class="seg" id="og"><button class="sgb on">예정</button>'+
 '<button class="sgb">미체결</button><button class="sgb">체결</button></div></div>'+
 '<div class="tip"><i class="fa-solid fa-robot"></i>주문은 자동 제출됩니다. 조회 전용 화면입니다.'+
 '</div><div id="otab"><div class="muted">불러오는 중…</div></div></div></div>';
 var tabs=$('og').children;[].forEach.call(tabs,function(b,i){b.onclick=function(){
  [].forEach.call(tabs,function(x){x.classList.remove('on');});b.classList.add('on');oTab(i);};});
 oTab(0);}
function oTab(i){var box=$('otab');box.innerHTML='<div class="muted">불러오는 중…</div>';
 var paths=STRATS.map(function(s){return s.key;});
 if(i===0){Promise.all(paths.map(function(k){return fetch('/'+k+'/api/orders/today')
  .then(function(r){return r.json();}).then(function(d){return (d||[]).map(function(o){
   o._s=labelOf(k);return o;});}).catch(function(){return [];});})).then(function(rs){
  var all=[].concat.apply([],rs);box.innerHTML=all.length?('<div style="overflow-x:auto"><table class="tbl"><thead><tr>'+
  '<th>전략</th><th>티커</th><th>구분</th><th>유형</th><th style="text-align:right">수량</th>'+
  '<th style="text-align:right">가격</th><th style="text-align:right">금액</th><th>설명</th></tr></thead><tbody>'+
  all.map(function(o){return '<tr><td>'+esc(o._s)+'</td><td><b>'+esc(o.ticker)+'</b></td>'+
  '<td><span class="tag '+(o.side==='buy'?'buy">매수':'sell">매도')+'</span></td><td>'+esc(o.order_type||'')+
  '</td><td style="text-align:right">'+o.qty+'</td><td style="text-align:right">'+money(o.price,2)+
  '</td><td style="text-align:right">'+money(o.amount,2)+'</td><td style="color:#9aa3b2;font-size:calc(11px*var(--ts-fs,1))">'+
  esc(o.desc||'')+'</td></tr>';}).join('')+'</tbody></table></div>'):
  '<div class="muted">오늘 예정 주문 없음 (장 시작 전이거나 조건 미충족)</div>';});}
 else if(i===1){Promise.all(paths.map(function(k){return fetch('/'+k+'/api/orders/pending')
  .then(function(r){return r.json();}).then(function(d){return ((d&&d.items)||[]).map(function(o){
   o._s=labelOf(k);return o;});}).catch(function(){return [];});})).then(function(rs){
  var all=[].concat.apply([],rs);box.innerHTML=all.length?('<div style="overflow-x:auto"><table class="tbl"><thead><tr>'+
  '<th>전략</th><th>티커</th><th>구분</th><th style="text-align:right">수량</th>'+
  '<th style="text-align:right">가격</th><th>주문번호</th><th>주문시각</th></tr></thead><tbody>'+
  all.map(function(o){return '<tr><td>'+esc(o._s)+'</td><td><b>'+esc(o.ticker)+'</b></td><td>'+
  esc(o.side_label||o.side||'')+'</td><td style="text-align:right">'+o.qty+
  '</td><td style="text-align:right">'+money(o.price,2)+'</td><td>'+esc(o.order_no||'')+'</td><td>'+
  esc((o.ord_dt||'')+' '+(o.ord_tmd||''))+'</td></tr>';}).join('')+'</tbody></table></div>'):
  '<div class="muted">미체결 주문 없음</div>';});}
 else{Promise.all(paths.map(function(k){return fetch('/'+k+'/api/trades?limit=500')
  .then(function(r){return r.json();}).then(function(d){return ((d&&d.items)||[]).map(function(o){
   o._s=labelOf(k);o._sk=k;return o;});}).catch(function(){return [];});})).then(function(rs){
  TRD=[].concat.apply([],rs);renderTradesTable();});}}
function labelOf(k){var s=STRATS.filter(function(x){return x.key===k;})[0];return s?s.label:k;}
/* ---------- 리스크 / 성과 / 모니터링 / 설정 ---------- */
function pgRisk(){var a=MET.account||{},ss=MET.strategies||[];
 var h='<div class="kpis">'+
  kpi('계좌 MDD','fa-arrow-trend-down','r',
   (a.mdd_pct==null?'수집중':Number(a.mdd_pct).toFixed(2)+'%'),'','최대낙폭(누적)')+
  kpi('현금 비중','fa-shield-halved','b',
   (a.cash_ratio==null?'—':Number(a.cash_ratio).toFixed(1)+'%'),'','방어 여력')+
  kpi('총 노출','fa-chart-pie','a',money(a.total_assets-a.cash>0?a.total_assets-a.cash:0),'','주식 평가분')+
  kpi('정지 전략','fa-circle-pause','n',(MET.automation.total-MET.automation.active)+' 개','','Kill Switch')+'</div>';
 h+='<div class="grid">'+card('전략별 리스크','fa-shield-halved',
  '<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>전략</th>'+
  '<th style="text-align:right">투입</th><th style="text-align:right">MDD</th>'+
  '<th style="text-align:right">수익률</th><th>상태</th></tr></thead><tbody>'+
  ss.map(function(s){return '<tr><td><b>'+esc(s.display_name)+'</b></td><td style="text-align:right">'+
  money(s.invested)+'</td><td style="text-align:right">'+(s.mdd_pct==null?'수집중':
  sP(s.mdd_pct))+'</td><td style="text-align:right">'+sP(s.return_pct)+'</td><td><span class="bdg '+
  (s.kill_switch?'stop':'run')+'">'+(s.kill_switch?'정지':'운용중')+'</span></td></tr>';}).join('')+
  '</tbody></table></div>')+'</div>';
 $('page').innerHTML=h;}
function pgPerf(){var ss=MET.strategies||[];
 var h='<div class="tip"><i class="fa-solid fa-rotate"></i>종료된 싸이클의 손익·거래내역입니다. '+
  '진행중 싸이클은 종료 후 집계됩니다.</div>'+
  '<div class="grid">'+card('전략별 실현손익','fa-chart-line',
  '<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>전략</th>'+
  '<th style="text-align:right">투입원금</th><th style="text-align:right">실현손익</th>'+
  '<th style="text-align:right">수익률</th><th style="text-align:right">승률</th>'+
  '<th style="text-align:right">완료 싸이클</th></tr></thead><tbody>'+ss.map(function(s){
  return '<tr><td><b>'+esc(s.display_name)+'</b></td><td style="text-align:right">'+money(s.invested)+
  '</td><td style="text-align:right">'+sM(s.realized_pnl)+'</td><td style="text-align:right">'+
  sP(s.return_pct)+'</td><td style="text-align:right">'+(s.win_rate==null?'—':s.win_rate.toFixed(1)+
  '%')+'</td><td style="text-align:right">'+s.cycles+'회</td></tr>';}).join('')+
  '</tbody></table></div>')+'</div>';
 ss.forEach(function(s){h+='<div class="grid">'+card('싸이클별 손익 · '+esc(s.display_name),
  'fa-rotate','<div id="cyc_'+s.strategy+'"><div class="muted">싸이클 불러오는 중…</div></div>')+'</div>';});
 $('page').innerHTML=h;
 ss.forEach(function(s){fetch('/'+s.strategy+'/api/cycles').then(function(r){return r.json();})
  .then(function(d){var it=(d&&d.items)||[];var sm=(d&&d.summary)||{};
   var box=$('cyc_'+s.strategy);
   if(!it.length){box.innerHTML='<div class="muted">완료된 싸이클 없음 (진행중이거나 미발생)</div>';return;}
   it.sort(function(a,b){return (b.end_date||'').localeCompare(a.end_date||'')||b.cycle_number-a.cycle_number;});
   box.innerHTML='<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>싸이클</th>'+
    '<th>티커</th><th>기간</th><th style="text-align:right">매수합</th>'+
    '<th style="text-align:right">매도합</th><th style="text-align:right">손익</th>'+
    '<th style="text-align:right">수익률</th><th></th></tr></thead><tbody>'+
    it.map(function(c){return '<tr><td><b>C'+c.cycle_number+'</b></td><td><b>'+esc(c.ticker)+
    '</b></td><td>'+esc(c.start_date)+' ~ '+esc(c.end_date)+'</td>'+
    '<td style="text-align:right">'+money(c.total_buy_amount)+'</td>'+
    '<td style="text-align:right">'+money(c.total_sell_amount)+'</td>'+
    '<td style="text-align:right">'+sM(c.profit)+'</td><td style="text-align:right">'+
    sP(c.profit_pct)+'</td><td style="text-align:right">'+
    '<button class="btn sm" onclick="cycTrades(\''+s.strategy+'\','+c.id+',\''+
    esc(c.ticker)+' C'+c.cycle_number+'\')">매수/매도</button></td></tr>';}).join('')+
    '</tbody></table></div><div id="cycd_'+s.strategy+'"></div>';
  }).catch(function(){$('cyc_'+s.strategy).innerHTML='<div class="muted">싸이클 로드 실패</div>';});});}
function cycTrades(k,cid,title){var box=$('cycd_'+k);
 box.innerHTML='<div class="muted">'+esc(title)+' 거래 불러오는 중…</div>';
 fetch('/'+k+'/api/cycles/'+cid+'/trades').then(function(r){return r.json();}).then(function(d){
  var tr=(d&&d.trades)||[];
  if(!tr.length){box.innerHTML='<div class="muted">'+esc(title)+' — 거래 내역 없음</div>';return;}
  var bs=tr.filter(function(t){return t.side==='buy';}),sl=tr.filter(function(t){return t.side==='sell';});
  var bSum=bs.reduce(function(a,t){return a+(t.amount||0);},0);
  var sSum=sl.reduce(function(a,t){return a+(t.amount||0);},0);
  box.innerHTML='<div style="padding:14px 18px;border-top:1px solid var(--line)">'+
   '<b style="font-size:calc(13px*var(--ts-fs,1))">'+esc(title)+' 매수/매도 상세</b> '+
   '<span style="color:var(--c2);font-size:calc(11.5px*var(--ts-fs,1))">매수 '+bs.length+'건 '+money(bSum)+
   ' · 매도 '+sl.length+'건 '+money(sSum)+' · 손익 '+sM(sSum-bSum)+'</span>'+
   '<div style="overflow-x:auto;margin-top:10px"><table class="tbl"><thead><tr><th>일자</th>'+
   '<th>구분</th><th>유형</th><th style="text-align:right">트렌치/회차</th>'+
   '<th style="text-align:right">가격</th><th style="text-align:right">수량</th>'+
   '<th style="text-align:right">금액</th></tr></thead><tbody>'+
   tr.map(function(t){return '<tr><td>'+esc(t.trade_date)+'</td><td><span class="tag '+
   (t.side==='buy'?'buy">매수':'sell">매도')+'</span></td><td>'+esc(t.order_type||'')+
   '</td><td style="text-align:right">'+(t.tranche_num!=null?('T'+t.tranche_num):(t.buy_seq!=null&&t.buy_seq!==''?('회차 '+esc(t.buy_seq)):'-'))+'</td>'+
   '<td style="text-align:right">'+money(t.price,2)+'</td><td style="text-align:right">'+t.qty+
   '</td><td style="text-align:right">'+money(t.amount,2)+'</td></tr>';}).join('')+
   '</tbody></table></div></div>';
 }).catch(function(){box.innerHTML='<div class="muted">거래 로드 실패</div>';});}
function pgMon(){var ss=MET.strategies||[];
 var h='<div class="grid g-2">';
 ss.forEach(function(s){h+='<div class="card"><div class="ch"><span class="ct">'+
  '<i class="fa-solid fa-desktop"></i>'+esc(s.display_name)+'</span><span class="bdg '+
  (s.kill_switch?'stop':'run')+'">'+(s.kill_switch?'정지':'운용중')+'</span></div>'+
  '<div id="mon_'+s.strategy+'"><div class="muted">로그 불러오는 중…</div></div></div>';});
 h+='</div>';$('page').innerHTML=h;
 ss.forEach(function(s){fetch('/'+s.strategy+'/api/logs?limit=12').then(function(r){
  return r.json();}).then(function(d){var it=(d&&d.items)||[];
  $('mon_'+s.strategy).innerHTML=it.length?it.map(function(l){
   var c=l.level==='ERROR'?'e':(l.level==='WARNING'?'w':'i');
   return '<div class="al"><span class="ad '+c+'"></span><span class="am">'+esc(l.message)+
   '<span class="at">'+esc(l.created_at||'')+'</span></span></div>';}).join(''):
   '<div class="muted">로그 없음</div>';}).catch(function(){
   $('mon_'+s.strategy).innerHTML='<div class="muted">로그 로드 실패</div>';});});}
function pgSys(){var ss=MET.strategies||[];var a=MET.account||{};
 var h='<div class="grid g-2"><div class="card"><div class="ch"><span class="ct">'+
  '<i class="fa-solid fa-building-columns"></i>계좌 정보</span></div>'+
  '<div style="padding:4px 0">'+
  row('공용 계좌 (KIS)','69567573 (실계좌 · real)')+row('총 평가자산',money(a.total_assets))+
  row('예수금',money(a.cash))+row('스냅샷 시각',esc((a.snapshot_at||'').replace('T',' ').slice(0,16)))+
  row('운용 전략',MET.automation.active+' / '+MET.automation.total)+
  (function(){var t=MET.toss||{};if(!t.configured)return '';
   var no=String(t.account_no||'');
   return row('토스 계좌 (조회 전용)',(no?no.slice(0,3)+'-**-**'+no.slice(-3):'-')+
    ' · 평가 '+money(t.eval_total)+' · 예수금 '+money(t.cash_usd)+
    (t.ts?(' · 갱신 '+esc(t.ts.slice(5,16))):'')+
    ' <button class="btn sm" style="margin-left:8px" onclick="tossRefresh()">지금 갱신</button>'+
    (t.error?('<br><span class="dn" style="font-size:calc(11px*var(--ts-fs,1))">'+esc(String(t.error).slice(0,90))+'</span>'):''));})()+
  '</div></div>'+
  '<div class="card"><div class="ch"><span class="ct"><i class="fa-solid fa-power-off"></i>'+
  '전략 가동 · 정지</span></div><div style="padding:4px 0">'+
  ss.map(function(s){return '<div style="display:flex;align-items:center;gap:10px;padding:13px 18px;'+
   'border-bottom:1px solid var(--line)"><b style="flex:1">'+esc(s.display_name)+'</b>'+
   '<span class="bdg '+(s.kill_switch?'stop':'run')+'">'+(s.kill_switch?'정지':'운용중')+'</span>'+
   '<button class="btn sm '+(s.kill_switch?'p':'dg')+'" onclick="togKill(\''+s.strategy+'\','+
   (s.kill_switch?'false':'true')+')">'+(s.kill_switch?'재가동':'정지')+'</button></div>';}).join('')+
  '</div></div></div>'+
  '<div class="card" style="margin-top:16px"><div class="ch"><span class="ct">'+
  '<i class="fa-solid fa-money-bill-transfer"></i>입출금 내역</span>'+
  '<span style="font-size:calc(11.5px*var(--ts-fs,1));color:var(--c2)">차트 입금·출금 기준</span></div>'+
  '<div class="form" style="grid-template-columns:repeat(4,1fr) auto">'+
  '<div class="fld"><label>일자</label><input id="cfDate" type="date"></div>'+
  '<div class="fld"><label>구분</label><select id="cfKind">'+
  '<option value="deposit">입금</option><option value="withdraw">출금</option></select></div>'+
  '<div class="fld"><label>금액 (USD)</label><input id="cfAmt" type="number" placeholder="예: 5000"></div>'+
  '<div class="fld"><label>메모</label><input id="cfMemo" placeholder="선택"></div>'+
  '<div class="fld" style="display:flex;align-items:flex-end"><button class="btn p" '+
  'onclick="addCashflow()">기록 추가</button></div></div>'+
  '<div id="cfList"><div class="muted">불러오는 중…</div></div></div>'+
  '<div class="card" style="margin-top:16px"><div class="ch"><span class="ct">'+
  '<i class="fa-solid fa-magnifying-glass-chart"></i>일일 점검</span>'+
  '<span style="font-size:calc(12.5px*var(--ts-fs,1));color:var(--c2)">매일 09:00 자동 · T값 · 싸이클</span>'+
  '<button class="btn sm p" style="margin-left:auto" onclick="tAuditRun()">'+
  '<i class="fa-solid fa-play"></i> 지금 점검</button></div>'+
  '<div id="tAuditBox"><div class="muted">불러오는 중…</div></div></div>'+
  '<div class="tip"><i class="fa-solid fa-shield-halved"></i>API 키는 이 화면에서 다루지 않습니다. '+
  '입출금은 직접 입력해야 반영됩니다.</div>';
 $('page').innerHTML=h;loadCashflow();loadTAudit();}
function loadTAudit(){fetch('/api/suite/t_audit').then(function(r){return r.json();})
 .then(function(d){renderTAudit(d);})
 .catch(function(){$('tAuditBox').innerHTML='<div class="muted">감사 결과 로드 실패</div>';});}
function _bdgFor(st){var m={ok:['run','일치'],mismatch:['part','불일치'],
 audit_failed:['stop','실행오류'],no_state:['stop','상태없음'],
 legacy:['part','참고(레거시)'],none:['stop','없음']};
 return m[st]||['stop',st||'?'];}
function _sectionOverallBadge(sec,emptyLbl){var ov=(sec&&sec.overall)||'none';
 var items=(sec&&sec.items)||[];var miss=items.filter(function(i){return i.status==='mismatch';}).length;
 var b=_bdgFor(ov);var lbl=ov==='ok'?(items.length?'전부 일치':emptyLbl):
  (ov==='mismatch'?('불일치 '+miss+'건'):b[1]);
 return ' <span class="bdg '+b[0]+'" style="margin-left:auto">'+lbl+'</span>';}
function _renderTItem(it){var b=_bdgFor(it.status);
 var det='';
 if(it.T_stored!=null){det='<div style="font-size:calc(11px*var(--ts-fs,1));color:var(--c2);margin-top:4px">'+
  'T(DB)='+it.T_stored+' · T(cum)='+it.T_recalc_cum+' · T(보유원가)='+it.T_from_holding+
  ' · cum_buy='+money(it.cum_buy)+' · cum_sell='+money(it.cum_sell)+
  ' · avg×qty='+money((it.avg_price||0)*(it.qty||0))+
  ' · B='+money(it.B,2)+' (seed '+money(it.seed)+' / A '+it.A+')</div>';}
 var reason=it.reason?('<div style="font-size:calc(11.5px*var(--ts-fs,1));color:var(--red);margin-top:5px;'+
  'background:var(--red-s);padding:8px 10px;border-radius:8px">'+esc(it.reason)+'</div>'):'';
 var note=it.note?('<div style="font-size:calc(11px*var(--ts-fs,1));color:var(--c2);margin-top:5px;'+
  'background:var(--bg);padding:7px 10px;border-radius:8px"><i class="fa-solid fa-circle-info"></i> '+
  esc(it.note)+'</div>'):'';
 return '<div style="padding:12px 18px;border-bottom:1px solid var(--line)">'+
  '<div style="display:flex;align-items:center;gap:10px">'+
  '<b style="font-size:calc(13px*var(--ts-fs,1))">'+esc(it.ticker||'-')+'</b>'+
  '<span style="color:var(--c2);font-size:calc(11px*var(--ts-fs,1))">싸이클 C'+(it.cycle||1)+'</span>'+
  '<span class="bdg '+b[0]+'" style="margin-left:auto">'+b[1]+'</span></div>'+
  det+reason+note+'</div>';}
function _renderCycItem(it){var b=_bdgFor(it.status);
 var scope=it.scope==='continuity'?' · 연속성':'';
 var det='';
 if(it.start_date){det='<div style="font-size:calc(11px*var(--ts-fs,1));color:var(--c2);margin-top:4px;word-break:break-all">'+
  esc(it.start_date)+' ~ '+esc(it.end_date)+' · trades '+(it.trades_count||0)+
  '건 (매수 '+(it.buys_count||0)+'/'+(it.buy_qty||0)+'주 '+money(it.buy_sum)+
  ' · 매도 '+(it.sells_count||0)+'/'+(it.sell_qty||0)+'주 '+money(it.sell_sum)+
  ') · history(buy '+money(it.history_buy)+' / sell '+money(it.history_sell)+
  ' / profit '+money(it.history_profit)+') · last_sell '+esc(it.last_sell_date||'-')+'</div>';}
 var reason=it.reason?('<div style="font-size:calc(11.5px*var(--ts-fs,1));color:var(--red);margin-top:5px;'+
  'background:var(--red-s);padding:8px 10px;border-radius:8px">'+esc(it.reason)+'</div>'):'';
 return '<div style="padding:12px 18px;border-bottom:1px solid var(--line)">'+
  '<div style="display:flex;align-items:center;gap:10px">'+
  '<b style="font-size:calc(13px*var(--ts-fs,1))">'+esc(it.ticker||'-')+'</b>'+
  '<span style="color:var(--c2);font-size:calc(11px*var(--ts-fs,1))">'+esc(it.strategy||'')+
  ' · C'+(it.cycle||'-')+scope+'</span>'+
  '<span class="bdg '+b[0]+'" style="margin-left:auto">'+b[1]+'</span></div>'+
  det+reason+'</div>';}
function renderTAudit(d){var box=$('tAuditBox');
 var ts=(d&&d.ts)||'';var overall=(d&&d.overall)||'none';
 if(!ts){box.innerHTML='<div class="muted">아직 감사 기록이 없습니다. "지금 검증"으로 1회 실행하세요.</div>';return;}
 var b=_bdgFor(overall);
 var head='<div style="padding:11px 18px;border-bottom:1px solid var(--line);'+
  'display:flex;align-items:center;gap:10px;font-size:calc(12px*var(--ts-fs,1));flex-wrap:wrap">'+
  '<span class="bdg '+b[0]+'">전체 '+b[1]+'</span>'+
  '<span style="color:var(--c2)">최근 검증 '+esc(ts.replace('T',' ').slice(0,19))+'</span>'+
  '<button class="btn sm" style="margin-left:auto" onclick="tAuditHistory()">'+
  '<i class="fa-solid fa-clock-rotate-left"></i> 이력</button></div>';
 var sec=(d&&d.sections)||null;
 if(!sec){var items=(d&&d.items)||[];
  box.innerHTML=head+(items.length?items.map(_renderTItem).join(''):
   '<div class="muted">대상 포트폴리오 없음</div>');return;}
 var tv=sec.t_value||{items:[]};var ci=sec.cycle_integrity||{items:[]};
 var hdr=function(title,icon,sec,empty){
  return '<div style="padding:10px 18px;background:var(--bg);'+
   'display:flex;align-items:center;gap:9px;font-size:calc(12px*var(--ts-fs,1));font-weight:700;color:var(--c1);'+
   'border-bottom:1px solid var(--line);border-top:1px solid var(--line)">'+
   '<i class="fa-solid '+icon+'" style="color:var(--c2)"></i>'+esc(title)+
   _sectionOverallBadge(sec,empty)+'</div>';};
 var tvHtml=hdr('T값 점검','fa-equals',tv,'대상 없음')+
  ((tv.items||[]).length?tv.items.map(_renderTItem).join(''):
   '<div class="muted">대상 포트폴리오 없음</div>');
 var ciHtml=hdr('싸이클 점검','fa-list-check',ci,'대상 없음')+
  ((ci.items||[]).length?ci.items.map(_renderCycItem).join(''):
   '<div class="muted">대상 싸이클 없음</div>');
 box.innerHTML=head+tvHtml+ciHtml;}
function tAuditRun(){toast('T값 감사 실행 중…');
 fetch('/api/suite/t_audit/run',{method:'POST'}).then(function(r){return r.json();})
  .then(function(d){renderTAudit(d);toast('감사 완료');})
  .catch(function(e){toast('감사 실패: '+e);});}
function tAuditHistory(){fetch('/api/suite/t_audit/history?limit=30').then(function(r){return r.json();})
 .then(function(d){var arr=(d&&d.items)||[];if(!arr.length){toast('이력 없음');return;}
  var lines=arr.slice().reverse().map(function(x){
   var sec=x.sections||null;var tvMiss=0,ciMiss=0;
   if(sec){tvMiss=((sec.t_value&&sec.t_value.items)||[]).filter(function(i){return i.status==='mismatch';}).length;
    ciMiss=((sec.cycle_integrity&&sec.cycle_integrity.items)||[]).filter(function(i){return i.status==='mismatch';}).length;}
   else{tvMiss=(x.items||[]).filter(function(i){return i.status==='mismatch';}).length;}
   return (x.ts||'').replace('T',' ').slice(0,16)+'  ['+x.overall+']  T불일치 '+tvMiss+
    '건 / 싸이클불일치 '+ciMiss+'건';}).join('\n');
  alert('T값 감사 이력 (최근 30회)\n\n'+lines);});}
function loadCashflow(){fetch('/api/suite/cashflow').then(function(r){return r.json();})
 .then(function(d){var e=d.entries||[],s=d.summary||{};var w=$('cfList');
  var head='<div style="padding:10px 18px;font-size:calc(12px*var(--ts-fs,1));color:var(--c1)">총 입금 <b class="up">'+
   money(s.total_deposit)+'</b> · 총 출금 <b class="dn">'+money(s.total_withdraw)+
   '</b> · 순입금 <b>'+money(s.net)+'</b></div>';
  if(!e.length){w.innerHTML=head+'<div class="muted">기록 없음 — 위에서 추가하세요</div>';return;}
  w.innerHTML=head+'<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>일자</th>'+
   '<th>구분</th><th style="text-align:right">금액</th><th>메모</th><th></th></tr></thead><tbody>'+
   e.map(function(x){var d=x.date;var ds=d.slice(0,4)+'-'+d.slice(4,6)+'-'+d.slice(6,8);
   return '<tr><td>'+ds+'</td><td><span class="tag '+(x.kind==='deposit'?'sell">입금':'buy">출금')+
   '</span></td><td style="text-align:right"><b>'+money(x.amount)+'</b></td><td>'+esc(x.memo||'')+
   '</td><td style="text-align:right"><button class="btn sm dg" onclick="delCashflow('+x.id+
   ')">삭제</button></td></tr>';}).join('')+'</tbody></table></div>';})
 .catch(function(){$('cfList').innerHTML='<div class="muted">원장 로드 실패</div>';});}
function addCashflow(){var dt=($('cfDate').value||'').replace(/-/g,'');
 var k=$('cfKind').value,amt=parseFloat($('cfAmt').value);
 if(dt.length!==8){toast('일자를 선택하세요');return;}
 if(isNaN(amt)||amt<=0){toast('금액을 입력하세요');return;}
 api('POST','/api/suite/cashflow',{date:dt,kind:k,amount:amt,memo:$('cfMemo').value||''})
  .then(function(){toast('입출금 기록 추가됨');$('cfAmt').value='';$('cfMemo').value='';
   loadCashflow();SER=null;}).catch(function(e){toast('실패: '+e);});}
function delCashflow(id){if(!confirm('이 입출금 기록을 삭제할까요?'))return;
 api('DELETE','/api/suite/cashflow/'+id).then(function(){toast('삭제됨');
  loadCashflow();SER=null;}).catch(function(e){toast('실패: '+e);});}
function row(k,v){return '<div style="display:flex;justify-content:space-between;padding:13px 18px;'+
 'border-bottom:1px solid var(--line);font-size:calc(12.5px*var(--ts-fs,1))"><span style="color:var(--c1)">'+k+
 '</span><b>'+v+'</b></div>';}
function togKill(k,act){if(!confirm(act?'이 전략을 정지(Kill Switch ON)할까요?':'이 전략을 재가동할까요?'))return;
 api('POST','/'+k+'/api/kill_switch?activate='+act).then(function(){
  toast('상태 변경됨');loadAll();}).catch(function(e){toast('실패: '+e);});}
/* ---------- 매매일지(블로그) ---------- */
var BJ=null;var BJMODE='journal';
function pgBlog(){
 var h='<div class="grid"><div class="card"><div class="ch">'+
  '<span class="ct"><i class="fa-solid fa-book"></i>매매일지</span>'+
  '<div class="seg" id="bjg" style="margin-left:auto"><button class="sgb'+(BJMODE==="journal"?" on":"")+
  '">일지</button><button class="sgb'+(BJMODE==="intro"?" on":"")+'">전략 소개</button></div></div>'+
  '<div id="bjCtl"></div>'+
  '<div class="tip" style="margin:14px 18px 0"><i class="fa-solid fa-circle-info"></i>'+
  '<span id="bjTip"></span></div>'+
  '<div id="bjBody"><div class="muted">불러오는 중…</div></div></div></div>';
 $('page').innerHTML=h;
 var tabs=$('bjg').children;[].forEach.call(tabs,function(b,i){b.onclick=function(){
  [].forEach.call(tabs,function(x){x.classList.remove('on');});b.classList.add('on');
  BJMODE=i===1?'intro':'journal';bjRenderMode();};});
 bjRenderMode();}
function bjRenderMode(){
 if(BJMODE==='intro'){
  $('bjCtl').innerHTML='';
  $('bjTip').innerHTML='전략 소개문입니다. 현재 설정(종목·목표%·트렌치) 기준으로 정리됩니다.';
  loadIntros();
 }else{
  $('bjCtl').innerHTML='<div style="padding:12px 18px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'+
   '<span style="font-size:calc(12px*var(--ts-fs,1));color:var(--c1)">일자</span>'+
   '<input type="date" id="bjDate" style="padding:7px 10px;border:1px solid var(--line);'+
   'border-radius:8px;font-family:inherit;font-size:calc(12.5px*var(--ts-fs,1))">'+
   '<button class="btn sm" onclick="loadJournal($(\'bjDate\').value.replace(/-/g,\'\'))">'+
   '<i class="fa-solid fa-rotate"></i> 조회</button></div>';
  $('bjTip').innerHTML='일자를 고르면 전략별로 그날 <b>매수·매도·싸이클</b>이 정리됩니다. 각 전략 블록의 <b>복사</b>를 눌러 네이버 블로그에 붙여넣으세요(전략당 1개 글).';
  loadJournal('');
 }}
function loadIntros(){
 if($('bjBody'))$('bjBody').innerHTML='<div class="muted">불러오는 중…</div>';
 fetch('/api/suite/strategy_intros').then(function(r){return r.json();}).then(function(d){
  var its=(d&&d.items)||[];renderIntros(its);})
  .catch(function(){if($('bjBody'))$('bjBody').innerHTML='<div class="muted">소개 로드 실패</div>';});}
function renderIntros(its){var box=$('bjBody');
 box.innerHTML=its.map(function(s,i){
  var head='<div style="display:flex;align-items:center;gap:10px;padding:12px 18px;'+
   'border-top:1px solid var(--line);background:var(--bg)"><b style="font-size:calc(13px*var(--ts-fs,1))">'+
   esc(s.display_name)+' · 전략 소개</b>'+
   '<button class="btn sm p" style="margin-left:auto" onclick="copyIntro('+i+')">'+
   '<i class="fa-solid fa-copy"></i> 복사</button></div>';
  var ta='<div style="padding:10px 18px 16px"><textarea id="bjintro'+i+'" readonly '+
   'style="width:100%;height:230px;box-sizing:border-box;padding:12px 14px;border:1px solid var(--line);'+
   'border-radius:10px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:calc(12.5px*var(--ts-fs,1));line-height:1.6;'+
   'color:var(--c0);background:#fbfcfe;resize:vertical;white-space:pre">'+esc(s.intro||'')+'</textarea></div>';
  return head+ta;}).join('');}
function copyIntro(i){var ta=$('bjintro'+i);if(!ta)return;ta.focus();ta.select();
 var done=function(){toast('복사됨 — 블로그 첫 글에 붙여넣기');};
 if(navigator.clipboard&&navigator.clipboard.writeText){
  navigator.clipboard.writeText(ta.value).then(done).catch(function(){
   try{document.execCommand('copy');done();}catch(e){toast('복사 실패 — 직접 선택해 복사');}});}
 else{try{document.execCommand('copy');done();}catch(e){toast('복사 실패 — 직접 선택해 복사');}}}
function loadJournal(ymd){
 if($('bjBody'))$('bjBody').innerHTML='<div class="muted">불러오는 중…</div>';
 fetch('/api/suite/journal'+(ymd?('?date='+ymd):'')).then(function(r){return r.json();})
  .then(function(d){BJ=d;
   var di=$('bjDate');if(di){var dd=d.date||'';di.value=dd.length===8?(dd.slice(0,4)+'-'+dd.slice(4,6)+'-'+dd.slice(6,8)):'';}
   renderJournal(d);})
  .catch(function(){if($('bjBody'))$('bjBody').innerHTML='<div class="muted">일지 로드 실패</div>';});}
function renderJournal(d){var box=$('bjBody');var ss=(d&&d.strategies)||[];
 var dd=d.date||'';var diso=dd.length===8?(dd.slice(0,4)+'-'+dd.slice(4,6)+'-'+dd.slice(6,8)):dd;
 var html='<div style="padding:8px 18px 4px;font-size:calc(12px*var(--ts-fs,1));color:var(--c2)">'+esc(diso)+
  ' 기준 · 최근 체결일 '+esc((d.latest_date||'').replace(/(\d{4})(\d{2})(\d{2})/,"$1-$2-$3"))+'</div>';
 html+=ss.map(function(s,i){
  var act=s.has_activity;
  var head='<div style="display:flex;align-items:center;gap:10px;padding:12px 18px;'+
   'border-top:1px solid var(--line);background:var(--bg)">'+
   '<b style="font-size:calc(13px*var(--ts-fs,1))">'+esc(s.display_name)+'</b>'+
   '<span style="color:var(--c2);font-size:calc(11.5px*var(--ts-fs,1))">매수 '+(s.buy_count||0)+'건 '+money(s.buy_sum)+
   ' · 매도 '+(s.sell_count||0)+'건 '+money(s.sell_sum)+
   ' · 보유 '+(s.holdings_qty||0)+'주'+
   ((s.cycles_ended&&s.cycles_ended.length)?(' · 🎯싸이클종료 '+s.cycles_ended.length):'')+'</span>'+
   '<button class="btn sm p" style="margin-left:auto" onclick="copyJournal('+i+')">'+
   '<i class="fa-solid fa-copy"></i> 복사</button></div>';
  var ta='<div style="padding:10px 18px 16px"><textarea id="bjtext'+i+'" readonly '+
   'style="width:100%;height:'+(act?'200px':'120px')+';box-sizing:border-box;padding:12px 14px;'+
   'border:1px solid var(--line);border-radius:10px;font-family:ui-monospace,Menlo,Consolas,monospace;'+
   'font-size:calc(12.5px*var(--ts-fs,1));line-height:1.6;color:var(--c0);background:#fbfcfe;resize:vertical;'+
   'white-space:pre">'+esc(s.text||'')+'</textarea></div>';
  return head+ta;}).join('');
 box.innerHTML=html;}
function copyJournal(i){var ta=$('bjtext'+i);if(!ta)return;ta.focus();ta.select();
 var done=function(){toast('복사됨 — 네이버 블로그에 붙여넣기 하세요');};
 if(navigator.clipboard&&navigator.clipboard.writeText){
  navigator.clipboard.writeText(ta.value).then(done).catch(function(){
   try{document.execCommand('copy');done();}catch(e){toast('복사 실패 — 직접 선택해 복사하세요');}});}
 else{try{document.execCommand('copy');done();}catch(e){toast('복사 실패 — 직접 선택해 복사하세요');}}}
/* ---------- VR (NH계좌) ---------- */
var VRCH={},VRPREV={},VRDATA=[];
function pgVr(){
 $('page').innerHTML='<div class="tip"><i class="fa-solid fa-circle-info"></i>'+
  '<span>토요일 <b>[미리보기]</b> → 표 확인 → <b>[예약 제출]</b> 하면 2주치 기간잔량 지정가 예약이 등록됩니다.</span></div>'+
  '<div id="vrBody"><div class="muted">불러오는 중…</div></div>';
 loadVr();}
function loadVr(){fetch('/vr/api/status').then(function(r){return r.json();})
 .then(function(d){renderVr(d);})
 .catch(function(){$('vrBody').innerHTML='<div class="muted">VR 상태 로드 실패</div>';});}
function vrFmtD(s){s=String(s||'');return s.length===8?(s.slice(0,4)+'.'+s.slice(4,6)+'.'+s.slice(6,8)):s;}
/* 예약 실패 경고 배너 — 자동제출이 조용히 실패하면 여기 빨갛게 뜬다.
   (2026-09 사고: 5기 22건 전량 거부가 로그에만 남아 일주일 넘게 몰랐다) */
function vrAlertBanner(g){var a=g&&g.alert;if(!a)return '';
 return '<div style="margin:12px 18px 0;background:var(--red-s);border:1px solid var(--red);'+
  'border-left:5px solid var(--red);border-radius:10px;padding:12px 15px">'+
  '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'+
  '<i class="fa-solid fa-triangle-exclamation" style="color:var(--red)"></i>'+
  '<b style="color:var(--red);font-size:calc(15px*var(--ts-fs,1))">예약 미접수 '+a.unresolved+'건</b>'+
  '<span style="font-size:calc(12px*var(--ts-fs,1));color:var(--c1)">'+esc(a.detail||'')+'</span></div>'+
  (a.reason?'<div style="font-size:calc(11.5px*var(--ts-fs,1));color:var(--c1);margin-top:6px">사유: '+esc(a.reason)+'</div>':'')+
  (a.at?'<div style="font-size:calc(10.5px*var(--ts-fs,1));color:var(--c2);margin-top:3px">마지막 시도 '+esc(a.at)+
   ' · 미리보기 후 예약 제출로 채우거나, 다음 토요일 자동제출을 기다리세요</div>':'')+
  '</div>';}
/* 체결 누락 감시 — NH 실보유와 모델(잔여×배수)의 차이가 기준에서 변하면 빨간 배너.
   체결을 빠짐없이 반영하면 차이는 그대로다. 변했다 = 반영 못 한 체결 또는 앱 직접 매매.
   (2026-09: 체결 조회가 고장나 있었는데 '체결 0건'이 정상처럼 보여 몰랐다) */
function vrQtyBanner(g){var q=g&&g.qty_audit;if(!q||q.state==='ok'||q.state==='no_snapshot')return '';
 var gid=esc(g.id),n=function(v){return (v>0?'+':'')+v;};
 var btn=function(lbl){return '<button class="btn sm" style="margin-left:auto" onclick="vrQtyReset(\''+gid+'\','+
  q.offset_now+')">'+lbl+'</button>';};
 if(q.state==='unset')
  return '<div style="margin:12px 18px 0;background:var(--amber-s);border:1px solid var(--amber);border-radius:10px;'+
   'padding:10px 15px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:calc(12.5px*var(--ts-fs,1))">'+
   '<i class="fa-solid fa-circle-info" style="color:var(--amber)"></i>체결 감시 기준 미설정 — 지금 NH '+q.acct_qty+
   '주 / 모델 '+q.model_qty_acct+'주 (차이 '+n(q.offset_now)+'주)'+btn('이 차이를 기준으로 설정')+'</div>';
 return '<div style="margin:12px 18px 0;background:var(--red-s);border:1px solid var(--red);'+
  'border-left:5px solid var(--red);border-radius:10px;padding:12px 15px">'+
  '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'+
  '<i class="fa-solid fa-triangle-exclamation" style="color:var(--red)"></i>'+
  '<b style="color:var(--red);font-size:calc(15px*var(--ts-fs,1))">보유수량 불일치 '+n(q.diff)+'주</b>'+
  '<span style="font-size:calc(12px*var(--ts-fs,1));color:var(--c1)">NH 실보유 '+q.acct_qty+'주 · 모델 '+q.model_qty_acct+
  '주 · 차이 기준 '+n(q.offset_base)+' → 지금 '+n(q.offset_now)+'</span>'+btn('의도한 매매면 기준 재설정')+'</div>'+
  '<div style="font-size:calc(11.5px*var(--ts-fs,1));color:var(--c1);margin-top:6px">반영 못 한 체결이 있거나 앱에서 직접 매매했습니다. '+
  '이대로 다음 주기를 산출하면 사다리가 라오어 표와 어긋납니다. 부분체결이면 남은 수량 체결 뒤 자동으로 풀립니다.</div></div>';}
/* 배수 증액 매집 진행 배너 — 매집 중에 NH 앱에서 산 주식은 사다리 예약으로 설명되지 않으면
   매집분으로 잡혀 모델에서 빠진다. 다 모이면 다음 예약부터 새 배수. */
function vrPendBanner(g){var p=g&&g.pending;if(!p)return '';
 var gid=esc(g.id),tone=p.ready?'green':'blue',nx=vrFmtD(p.next_submit);
 var head=p.ready
  ?('매집 완료 '+p.accum+' / '+p.target+'주 — 다음 예약('+nx+' 토)부터 ×'+p.to+'로 나갑니다')
  :('배수 증액 매집 중 ×'+p.from+' → ×'+p.to+' · '+p.accum+' / '+p.target+'주'+
    ' <span style="font-weight:400">(남은 '+p.left+'주'+(p.left_cost!=null?' ≈ '+money(p.left_cost):'')+')</span>');
 var sub=p.ready
  ?(g.auto_submit?'토요일 자동제출이 새 배수로 산출합니다. ':'미리보기 → 예약 제출 시 새 배수로 산출됩니다. ')+
   '이번 주기에 걸린 예약은 기존 수량 그대로입니다.'
  :'NH 앱에서 '+esc(g.ticker)+'를 직접 사면 매집분으로 잡혀 VR 모델에 들어가지 않습니다 (사다리 예약 체결은 제외). '+
   '다 모이면 다음 예약('+nx+')부터 ×'+p.to+' · 덜 모이면 그 주기는 ×'+p.from+'로 나갑니다. '+
   '반영: 매일 10:05·22:05 자동 또는 [체결 반영]';
 sub+='<br>×'+p.to+' 기준 Pool 필요 <b>'+money(p.pool_required_after)+'</b> (지금 '+money(p.pool_actual)+')'+
  (p.deposit_left>0?' · 입금 필요(추정) <b>'+money(p.deposit_left)+'</b>':' · Pool 충족');
 return '<div style="margin:12px 18px 0;background:var(--'+tone+'-s);border:1px solid var(--'+tone+');'+
  'border-left:5px solid var(--'+tone+');border-radius:10px;padding:12px 15px">'+
  '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'+
  '<i class="fa-solid '+(p.ready?'fa-circle-check':'fa-cart-plus')+'" style="color:var(--'+tone+')"></i>'+
  '<b style="color:var(--'+tone+');font-size:calc(14.5px*var(--ts-fs,1))">'+head+'</b>'+
  '<button class="btn sm" style="margin-left:auto" onclick="vrMultCancel(\''+gid+'\')">증액 취소</button></div>'+
  '<div style="font-size:calc(11.5px*var(--ts-fs,1));color:var(--c1);margin-top:6px">'+sub+'</div></div>';}
function vrMultUp(gid){var g=(VRDATA||[]).filter(function(x){return x.id===gid;})[0];if(!g)return;
 if(g.pending){toast('이미 증액 매집 중입니다');return;}
 var v=prompt(g.name+' 배수 증액\n지금 ×'+g.mult+' → 몇 배수로?',g.mult+1);if(v==null)return;
 var to=parseInt(v,10);if(!(to>g.mult)){toast('지금 배수보다 큰 값을 넣으세요');return;}
 fetch('/vr/api/gisu/'+gid+'/mult_plan?to='+to).then(function(r){return r.json().then(function(j){
  if(!r.ok)throw (j&&j.detail)||'산출 실패';return j;});})
 .then(function(p){var cf=function(x){return x<0?('인출 '+money(-x)):('적립 '+money(x));};
  var msg=p.name+' 배수 ×'+p.from+' → ×'+p.to+' 증액\n\n'+
   '① 추가 매수: '+p.ticker+' '+p.add_qty+'주  (모델잔여 '+p.model_qty+' × '+p.delta+')\n'+
   '    '+(p.add_cost!=null?('지금가 '+money(p.close,2)+' 기준 약 '+money(p.add_cost)):'현재가 미확인')+'\n'+
   '② Pool: 모델 '+money(p.pool_model)+' × '+p.to+' = '+money(p.pool_required_after)+' 필요  (지금 실제 '+money(p.pool_actual)+')\n'+
   '③ 입금 필요 추정: 약 '+money(p.deposit_est)+'  (매수대금 + Pool 부족분)\n'+
   '④ 적용: '+p.add_qty+'주가 다 모이면 다음 예약('+vrFmtD(p.next_submit)+' 토)부터 1칸 '+p.step_qty_from+'주 → '+p.step_qty_to+'주\n'+
   '    덜 모이면 그 주기는 '+p.step_qty_from+'주로 나가고, 다 모인 다음 주기부터 적용\n'+
   (p.cashflow_from?('⑤ 주기당 '+cf(p.cashflow_from)+' → '+cf(p.cashflow_to)+'\n'):'')+
   '\n[확인]을 누르면 매집 중이 됩니다. 그다음 NH 앱에서 '+p.ticker+'를 직접 사세요.\n'+
   '사다리 예약 체결이 아닌 매수는 자동으로 매집분으로 잡혀 VR 모델에 들어가지 않습니다.';
  if(!confirm(msg))return;
  return fetch('/vr/api/gisu/'+gid+'/mult_plan',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({to:to})}).then(function(r){return r.json().then(function(j){
    if(!r.ok)throw (j&&j.detail)||'시작 실패';toast('매집 시작 — NH 앱에서 '+j.add_qty+'주를 사세요');loadVr();});});})
 .catch(function(e){toast('실패: '+e);});}
function vrMultCancel(gid){var g=(VRDATA||[]).filter(function(x){return x.id===gid;})[0];
 var p=g&&g.pending;if(!p)return;
 if(!confirm('배수 ×'+p.to+' 증액을 취소할까요?'+(p.accum?('\n\n이미 산 '+p.accum+'주는 계좌에 그대로 남습니다.\n'+
  '취소 후 수량 감시 배너에서 기준을 재설정하거나 직접 매도하세요.'):'')))return;
 fetch('/vr/api/gisu/'+gid+'/mult_plan',{method:'DELETE'}).then(function(r){if(!r.ok)throw 0;return r.json();})
  .then(function(){toast('배수 증액 취소됨');loadVr();}).catch(function(){toast('취소 실패');});}
function vrQtyReset(gid,v){
 if(!confirm('체결 감시 기준을 지금 차이('+(v>0?'+':'')+v+'주)로 재설정합니다.\n\n직접 매매했거나 원인을 확인한 경우에만 누르세요. 체결 누락이면 재설정해도 모델은 여전히 틀립니다.'))return;
 fetch('/vr/api/gisu/'+gid+'/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({qty_offset:v})}).then(function(r){if(!r.ok)throw 0;toast('기준 재설정됨');loadVr();})
  .catch(function(){toast('재설정 실패');});}
/* 모델 Pool(×배수) vs 실제 보유 Pool 과부족.
   Pool 은 현금만이 아니라 RP·원화자산·타종목까지 포함한 '주식 외 자산' 전부다.
   NH API 는 해외주식만 조회돼 RP·원화분이 안 보이므로 기타자산은 수동입력분을 더한다. */
function vrCashBox(g){var c=g&&g.cash;if(!c)return '';
 var short=c.short,tone=short?'red':'blue';
 var box=function(lbl,val,sub,tone){return '<div style="flex:1;min-width:160px;background:var(--'+tone+
  '-s);border:1px solid var(--'+tone+');border-radius:10px;padding:10px 13px">'+
  '<div style="font-size:calc(11.5px*var(--ts-fs,1));color:var(--c1)">'+lbl+'</div>'+
  '<b style="font-size:calc(17px*var(--ts-fs,1));color:var(--'+tone+')">'+val+'</b>'+
  (sub?'<div style="font-size:calc(10.5px*var(--ts-fs,1));color:var(--c2);margin-top:2px">'+sub+'</div>':'')+'</div>';};
 var fxs=c.fx?('@'+money(c.fx,2)+'원'):'환율 미확인';
 /* NH 는 예수금을 원화/외화 두 벌로 주는데 같은 지갑이라 더하면 두 번 센다.
    same_pot 이면 원화는 '상당액' 으로만 병기. */
 var mix='예수금 '+money(c.cash_usd)+' + 기타자산 '+money(c.ext_assets)+
   (c.ext_krw?(' (원화 '+Math.round(c.ext_krw).toLocaleString()+'원 → '+money(c.ext_krw_usd)+' '+fxs+')'):'');
 return '<div style="display:flex;gap:10px;flex-wrap:wrap;padding:0 18px 6px">'+
  box('필요 Pool <span style="font-size:calc(10px*var(--ts-fs,1))">(모델×배수)</span>',money(c.pool_required),
      '모델 '+money(c.pool_model)+' × '+g.mult+'배수','amber')+
  box('실제 보유 Pool <span style="font-size:calc(10px*var(--ts-fs,1))">(달러환산)</span>',money(c.pool_actual),mix,'blue')+
  box(short?'부족분':'여유분',(short?'−':'+')+money(Math.abs(c.diff)),
      short?'Pool 이 모델보다 모자랍니다':'모델 Pool 충족',tone)+
  '</div>'+
  '<div style="padding:0 18px 12px;font-size:calc(10.5px*var(--ts-fs,1));color:var(--c2)">'+
  'Pool = 주식 외 자산 전부(현금·RP·원화·타종목). NH API 는 해외주식만 조회돼 '+
  'RP·원화·국내자산이 안 잡히니 <b>기타자산</b> 칸에 직접 넣어주세요. '+
  '이번 매수 사다리 소요 '+money(c.need_usd)+' ('+c.need_steps+'단) · NH 주문가능금액 '+
  money(c.order_amt)+'</div>';}
function renderVr(d){var gs=(d&&d.gisu)||[];VRDATA=gs;
 $('vrBody').innerHTML=gs.map(function(g){var gid=g.id;
  var info='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;padding:14px 18px;font-size:calc(13.5px*var(--ts-fs,1))">'+
   [['주차',g.week_no+'주차'],['기간',vrFmtD(g.cyc_start)+' ~ '+vrFmtD(g.cyc_end)],
    ['V',money(g.v)],['밴드',money(g.band_lo)+' ~ '+money(g.band_hi)],
    ['모델 잔여',g.model_qty+'주'],['Pool <span style="font-size:calc(10.5px*var(--ts-fs,1))">(모델)</span>',money(g.pool_now)],
    ['계좌',esc(String(g.acct_no).slice(0,3)+'-**-**'+String(g.acct_no).slice(-3))],
    ['예약',g.reserved_this_week+'건']]
   .concat(g.snapshot?[['보유',g.snapshot.qty+'주'],
    ['평가',money(g.snapshot.eval_usd)+' <span style="color:var(--c2);font-size:calc(11px*var(--ts-fs,1))">@'+
     money(g.snapshot.close,2)+'</span>']]:[])
   .concat(g.cash?[['총자산 <span style="font-size:calc(10.5px*var(--ts-fs,1))">(달러환산)</span>',
    money(g.cash.assets_usd)+' <span style="color:var(--c2);font-size:calc(11px*var(--ts-fs,1))">주식+예수금</span>']]:[])
   .map(function(x){
    return '<div><div style="color:var(--c2);font-size:calc(12px*var(--ts-fs,1))">'+x[0]+'</div><b>'+x[1]+'</b></div>';}).join('')+'</div>'
   +vrCashBox(g);
  var set='<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;padding:0 18px 12px;font-size:calc(11.5px*var(--ts-fs,1))">'+
   [['배수','vrM_'+gid,g.mult],['현금흐름/주기(인출−)','vrC_'+gid,g.cashflow],
    ['G','vrG_'+gid,g.g],['매도단수','vrS_'+gid,g.sell_steps],
    ['기타자산 $ (RP·타종목)','vrX_'+gid,((g.cash&&g.cash.ext_usd)||0)],
    ['기타자산 원화 (자동환산)','vrXK_'+gid,((g.cash&&g.cash.ext_krw)||0)]].map(function(x){
    return '<label style="display:flex;flex-direction:column;gap:3px;color:var(--c2)">'+x[0]+
     '<input id="'+x[1]+'" type="number" step="any" value="'+x[2]+'" style="width:96px;padding:6px 8px;'+
     'border:1px solid var(--line);border-radius:7px;font-family:inherit"></label>';}).join('')+
   '<label style="display:flex;flex-direction:column;gap:3px;color:var(--c2)">자동 제출(토)'+
   '<select id="vrA_'+gid+'" style="padding:6px 8px;border:1px solid var(--line);border-radius:7px;font-family:inherit">'+
   '<option value="0"'+(g.auto_submit?'':' selected')+'>수동</option>'+
   '<option value="1"'+(g.auto_submit?' selected':'')+'>자동</option></select></label>'+
   '<button class="btn sm" onclick="vrSaveSet(\''+gid+'\')">설정 저장</button>'+
   '<button class="btn sm" onclick="vrMultUp(\''+gid+'\')"><i class="fa-solid fa-cart-plus"></i> 배수 증액</button>'+
   '<button class="btn sm" onclick="vrSync(\''+gid+'\')"><i class="fa-solid fa-rotate"></i> 체결 반영</button>'+
   '<button class="btn sm p" onclick="vrPreview(\''+gid+'\')"><i class="fa-solid fa-table-list"></i> 미리보기</button></div>';
  return '<div class="grid"><div class="card"><div class="ch"><span class="ct">'+
   '<i class="fa-solid fa-scale-balanced"></i>'+esc(g.name)+' · ×'+g.mult+'배수</span>'+
   '<span class="bdg '+(g.kill_switch?'stop':'run')+'" style="margin-left:auto">'+(g.kill_switch?'정지':'운용중')+'</span></div>'+
   vrAlertBanner(g)+vrPendBanner(g)+vrQtyBanner(g)+info+set+
   '<div class="cw" style="height:240px"><canvas id="vrch_'+gid+'"></canvas></div>'+
   '<div id="vrprev_'+gid+'"></div><div id="vrres_'+gid+'"></div></div></div>';
 }).join('');
 gs.forEach(function(g){fetch('/vr/api/gisu/'+g.id+'/graph').then(function(r){return r.json();})
  .then(function(gd){drawVrChart(g.id,gd);}).catch(function(){});
  renderVrLedger(g.id);});}
function renderVrLedger(gid){fetch('/vr/api/gisu/'+gid).then(function(r){return r.json();})
 .then(function(d){var rows=(d.reserved||[]).slice(-24).reverse();var box=$('vrres_'+gid);if(!box)return;
  if(!rows.length){box.innerHTML='';return;}
  box.innerHTML='<div style="padding:0 18px 14px"><details><summary style="cursor:pointer;font-size:calc(11.5px*var(--ts-fs,1));'+
   'color:var(--blue);font-weight:600">예약 원장 ('+rows.length+'건)</summary>'+
   '<div style="overflow-x:auto;margin-top:8px"><table class="tbl"><thead><tr><th>주차</th><th>구분</th>'+
   '<th style="text-align:right">가격</th><th style="text-align:right">수량</th><th>기간</th><th>NH접수번호</th><th>상태</th></tr></thead><tbody>'+
   rows.map(function(o){return '<tr><td>'+o.week_no+'</td><td><span class="tag '+(o.side==='buy'?'buy">매수':'sell">매도')+
    '</span></td><td style="text-align:right">'+money(o.price,2)+'</td><td style="text-align:right">'+o.qty_acct+
    '</td><td>'+vrFmtD(o.start_dt)+'~'+vrFmtD(o.end_dt)+'</td><td>'+esc(o.nh_order_no||'-')+'</td><td>'+esc(o.status)+'</td></tr>';}).join('')+
   '</tbody></table></div></details></div>';});}
function drawVrChart(gid,gd){var el=$('vrch_'+gid);if(!el)return;
 var rows=(gd&&gd.weekly)||[];if(!rows.length)return;
 var labels=rows.map(function(r){return r.week_no+'주';});
 var ev=rows.map(function(r){return r.eval_amt;});
 if(gd.live_eval!=null){var idx=rows.findIndex(function(r){return r.week_no===gd.current_week;});
  if(idx>=0&&ev[idx]==null)ev[idx]=gd.live_eval;}
 if(VRCH[gid]){VRCH[gid].destroy();}
 VRCH[gid]=new Chart(el,{type:'line',data:{labels:labels,datasets:[
  {label:'평가금',data:ev,borderColor:'#e5484d',borderWidth:2.4,pointRadius:3,tension:.15,spanGaps:true},
  {label:'최소',data:rows.map(function(r){return r.band_lo;}),borderColor:'#6c5ce7',borderDash:[6,4],borderWidth:1.6,pointRadius:0,spanGaps:true},
  {label:'최대',data:rows.map(function(r){return r.band_hi;}),borderColor:'#6c5ce7',borderDash:[6,4],borderWidth:1.6,pointRadius:0,spanGaps:true}]},
  options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
   plugins:{legend:{position:'bottom',labels:{usePointStyle:true,boxWidth:7,font:{size:11*TSFS}}},
    tooltip:{backgroundColor:'#1a2233',padding:10,cornerRadius:8}},
   scales:{x:{grid:{display:false},ticks:{color:'#9aa3b2',font:{size:10*TSFS}}},
    y:{grid:{color:'#eef1f6'},ticks:{color:'#9aa3b2',font:{size:10*TSFS},
     callback:function(v){return '$'+(v/1000).toFixed(0)+'k';}}}}}});}
function vrSaveSet(gid){var b={mult:parseInt($('vrM_'+gid).value),cashflow:parseFloat($('vrC_'+gid).value),
  g:parseFloat($('vrG_'+gid).value),sell_steps:parseInt($('vrS_'+gid).value),
  ext_assets:parseFloat($('vrX_'+gid).value),
  ext_assets_krw:parseFloat($('vrXK_'+gid).value),
  auto_submit:parseInt($('vrA_'+gid).value)};
 fetch('/vr/api/gisu/'+gid+'/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(b)}).then(function(r){if(!r.ok)throw 0;return r.json();})
  .then(function(){toast('설정 저장됨 (다음 미리보기부터 반영)');loadVr();})
  .catch(function(){toast('설정 저장 실패');});}
function vrSync(gid){toast('체결 동기화 중…');
 fetch('/vr/api/gisu/'+gid+'/sync',{method:'POST'}).then(function(r){return r.json();})
  .then(function(d){toast('동기화: 신규 체결 '+(d.new_fills||0)+'건');loadVr();})
  .catch(function(){toast('동기화 실패');});}
function vrPreview(gid,qs){var box=$('vrprev_'+gid);box.innerHTML='<div class="muted">산출 중…</div>';
 fetch('/vr/api/gisu/'+gid+'/preview'+(qs||'')).then(function(r){
  if(!r.ok)return r.json().then(function(e){throw (e&&e.detail)||'산출 실패';});return r.json();})
 .then(function(p){VRPREV[gid]=p;
  var mk=function(rows,label,cls){return '<div style="flex:1;min-width:260px"><b style="font-size:calc(12px*var(--ts-fs,1))">'+label+
   ' ('+rows.length+'단)</b><div style="overflow-x:auto"><table class="tbl"><thead><tr><th>#</th>'+
   '<th style="text-align:right">가격</th><th style="text-align:right">계좌수량</th>'+
   '<th style="text-align:right">모델잔여</th><th style="text-align:right">Pool</th></tr></thead><tbody>'+
   rows.map(function(r){return '<tr><td>'+r.step+'</td><td style="text-align:right" class="'+cls+'"><b>'+
    money(r.price,2)+'</b></td><td style="text-align:right">'+r.qty_acct+'</td><td style="text-align:right">'+
    r.remain_after+'</td><td style="text-align:right">'+money(r.pool_after,2)+'</td></tr>';}).join('')+
   '</tbody></table></div></div>';};
  box.innerHTML='<div style="padding:12px 18px;border-top:1px solid var(--line)">'+
   '<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:calc(12px*var(--ts-fs,1));margin-bottom:10px">'+
   '<b>'+p.week_no+'주차 제안</b>'+
   '<span>기간 <input id="vrDs_'+gid+'" value="'+p.cyc_start+'" style="width:86px"> ~ <input id="vrDe_'+gid+'" value="'+p.cyc_end+'" style="width:86px"></span>'+
   '<span>E(마감평가금) <input id="vrE_'+gid+'" value="'+p.e_used+'" style="width:96px">'+
   (p.close_info?' <span style="color:var(--c2)">(자동: '+vrFmtD(p.close_info.date)+' 종가 $'+p.close_info.close+' × '+p.current.model_qty+'주)</span>':'')+'</span>'+
   '<button class="btn sm" onclick="vrPreviewWith(\''+gid+'\')">재산출</button></div>'+
   '<div class="hl" style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:12px;font-size:calc(14px*var(--ts-fs,1))">'+
   '<span>V <b>'+money(p.v)+'</b></span><span>밴드 <b>'+money(p.band_lo)+' ~ '+money(p.band_hi)+'</b></span>'+
   '<span>Pool <b>'+money(p.pool_start)+'</b></span><span>배수 <b>×'+p.mult+'</b>'+
   (p.mult_switch?(' <span style="color:var(--green);font-size:calc(12px*var(--ts-fs,1))">(증액 적용 ×'+p.mult_switch.from+' → ×'+p.mult_switch.to+')</span>'):'')+
   '</span></div>'+
   '<div style="display:flex;gap:14px;flex-wrap:wrap">'+mk(p.buys,'매수','dn')+mk(p.sells,'매도','up')+'</div>'+
   '<div style="margin-top:12px;display:flex;gap:8px;align-items:center">'+
   '<button class="btn p" onclick="vrSubmit(\''+gid+'\')"><i class="fa-solid fa-paper-plane"></i> 예약 제출 ('+ (p.buys.length+p.sells.length) +'건)</button>'+
   '<span style="font-size:calc(11px*var(--ts-fs,1));color:var(--c2)">제출 전 팬딩 표와 가격(±$0.01)·수량을 대조하세요</span></div></div>';})
 .catch(function(e){box.innerHTML='<div class="muted">'+esc(String(e))+'</div>';});}
function vrPreviewWith(gid){var e=$('vrE_'+gid).value,s=$('vrDs_'+gid).value,en=$('vrDe_'+gid).value;
 vrPreview(gid,'?e='+encodeURIComponent(e)+'&start='+s+'&end='+en);}
function vrSubmit(gid){var p=VRPREV[gid];if(!p){toast('먼저 미리보기를 실행하세요');return;}
 var s=$('vrDs_'+gid).value,en=$('vrDe_'+gid).value;
 var rows=p.buys.map(function(r){return {side:'buy',price:r.price,qty_acct:r.qty_acct};})
  .concat(p.sells.map(function(r){return {side:'sell',price:r.price,qty_acct:r.qty_acct};}));
 if(!confirm(p.week_no+'주차 예약 '+rows.length+'건 (매수 '+p.buys.length+'/매도 '+p.sells.length+')\n기간 '+s+'~'+en+'\nNH 계좌에 실제 예약주문이 등록됩니다. 진행할까요?'))return;
 if(!confirm('최종 확인: 팬딩 표와 대조하셨나요? 제출 후 취소는 NH 예약취소로만 가능합니다.'))return;
 fetch('/vr/api/gisu/'+gid+'/submit',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({week_no:p.week_no,cyc_start:s,cyc_end:en,e_used:parseFloat($('vrE_'+gid).value),
   v:p.v,band_lo:p.band_lo,band_hi:p.band_hi,pool_start:p.pool_start,rows:rows,mult:p.mult})})
 .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
 .then(function(x){if(!x.ok){toast('제출 실패: '+((x.d&&x.d.detail)||''));return;}
  toast('예약 '+x.d.submitted+'건 제출'+(x.d.failed?(' · 실패 '+x.d.failed+'건 — 원장 확인'):' · 주기 전환 완료'));
  VRPREV[gid]=null;loadVr();})
 .catch(function(){toast('제출 중 오류');});}
/* ---------- 라우터 ---------- */
function skeleton(){ /* 로딩 중에도 레이아웃을 먼저 그려 체감 속도 향상 */
 var box=function(h){return '<div class="card" style="height:'+h+'px;background:'+
  'linear-gradient(90deg,#f4f6fa 25%,#eef1f7 37%,#f4f6fa 63%);background-size:400% 100%;'+
  'animation:sk 1.2s ease-in-out infinite"></div>';};
 return '<style>@keyframes sk{0%{background-position:100% 50%}100%{background-position:0 50%}}</style>'+
  '<div class="kpis">'+[1,2,3,4,5,6].map(function(){return box(92);}).join('')+'</div>'+
  '<div class="grid g-3-1">'+box(300)+box(300)+'</div>'+
  '<div class="grid">'+box(220)+'</div>';}
function render(){if(!MET){$('page').innerHTML=skeleton();return;}
 ({dash:pgDash,strat:pgStrat,port:pgPort,order:pgOrder,risk:pgRisk,perf:pgPerf,
   vr:pgVr,blog:pgBlog,mon:pgMon,sys:pgSys}[PAGE]||pgDash)();}
function loadAll(){
 if(!MET)$('page').innerHTML=skeleton();
 var ctl=('AbortController' in window)?new AbortController():null;
 var to=setTimeout(function(){if(ctl)ctl.abort();},12000);   /* 12초 넘으면 중단 후 재시도 안내 */
 return fetch('/api/suite/metrics',ctl?{signal:ctl.signal}:undefined)
 .then(function(r){return r.json();})
 .then(function(d){clearTimeout(to);
  if(d&&d.warming){                      /* 서버 예열 중 — 스켈레톤 유지하고 재조회 */
   if(!MET)$('page').innerHTML=skeleton();
   setTimeout(loadAll,1500);return;}
  MET=d;var au=d.automation||{};
  $('st').className='st'+(au.running?'':' off');
  $('stt').textContent=au.running?'정상 운영중':'정지 상태';
  var cache=d.cache||{};
  $('sbst').innerHTML='가동 '+au.active+'/'+au.total+' 전략<br>갱신 '+
   esc((d.generated_at||'').replace('T',' ').slice(11,19))+
   (cache.age_sec>30?(' <span style="color:var(--c2)">('+Math.round(cache.age_sec)+'초 전)</span>'):'');
  render();})
 .catch(function(){clearTimeout(to);
  if(!MET)$('page').innerHTML='<div class="empty"><i class="fa-solid fa-plug-circle-exclamation"></i>'+
   '<div class="t">데이터 로드 지연</div><div class="s">잠시 후 자동 재시도합니다</div>'+
   '<button class="btn sm p" onclick="loadAll()">다시 시도</button></div>';
  setTimeout(function(){if(!MET)loadAll();},4000);});}
function tick(){$('dt').textContent=new Date().toLocaleString('ko-KR',{hour12:false});}
$('hamb').onclick=function(){$('sb').classList.toggle('open');$('scrim').classList.toggle('show');};
$('scrim').onclick=function(){$('sb').classList.remove('open');$('scrim').classList.remove('show');};
$('refresh').onclick=function(){SER=null;loadAll();};
buildNav();tick();setInterval(tick,1000);loadAll();
setInterval(function(){if(PAGE==='dash'||PAGE==='mon')loadAll();},60000);
</script>
<!-- TS_V3_FOOT_START -->
<script id="ts-workspace-js">/* V3 presentation adapter. Load AFTER the original app script.
 * Moves existing nodes: never replaces original controls, data or handlers.
 * The original render remains the only business renderer. */
(function(){
 'use strict';
 if(typeof window.render!=='function'||window.__tsWorkspaceInstalled)return;
 window.__tsWorkspaceInstalled=true;
 var originalRender=window.render, enabled=true, previousPage=null;
 var disclosure={};
 function el(tag,cls,text){var n=document.createElement(tag);if(cls)n.className=cls;if(text)n.textContent=text;return n;}
 function link(text,page,cls){var b=el('button',cls||'ws-link',text);b.type='button';b.onclick=function(){window.go(page);};return b;}
 function openGroup(id,title,sub,nodes){var d=el('details','ws-disclosure');d.id=id;d.open=!!disclosure[id];var sum=el('summary');var left=el('span');left.append(el('b','',title),el('small','',sub));sum.append(left,el('span','ws-expand','＋'));d.append(sum);var contents=el('div','ws-disclosure-body');nodes.forEach(n=>contents.append(n));d.append(contents);d.addEventListener('toggle',function(){disclosure[id]=d.open;if(d.open)requestAnimationFrame(function(){[window.C1,window.C2,window.C3].forEach(c=>{if(c&&c.resize)c.resize();});});});return d;}
 function enhance(){
  var page=document.getElementById('page');document.body.classList.toggle('ws-v3',enabled);
  document.querySelectorAll('.ws-mobile button').forEach(b=>{b.classList.toggle('selected',b.dataset.page===window.PAGE);});
  if(!enabled||!window.MET||window.PAGE!=='dash')return;
  if(page.querySelector('.ws-dashboard'))return;
  var kpis=page.querySelector('.kpis'),account=page.querySelector('#acctSeg');
  var ids=['cw1','slist','lg2','rbars','psum','phold','ptr','palert'];
  if(!kpis||kpis.children.length!==6||!account||ids.some(id=>!document.getElementById(id)))return;
  var cards={};ids.forEach(id=>{cards[id]=document.getElementById(id).closest('.card');});
  if(ids.some(id=>!cards[id]))return;
  // Retain all original content. Collect references before changing layout.
  var accountRow=account.parentElement,ks=Array.from(kpis.children),oldChildren=Array.from(page.children);
  var shell=el('div','ws-dashboard');
  var heading=el('section','ws-heading');var intro=el('div');intro.append(el('span','ws-overline','PORTFOLIO WORKSPACE'),el('h1','','투자 현황'),el('p','','자산의 흐름과 전략의 상태를 한눈에 확인하세요.'));heading.append(intro);accountRow.classList.add('ws-account');heading.append(accountRow);shell.append(heading);
  var main=el('div','ws-main');var wealth=el('section','ws-wealth');
  var wealthTop=el('div','ws-wealth-top');ks[0].classList.add('ws-main-asset');ks[2].classList.add('ws-main-return');wealthTop.append(ks[0],ks[2]);wealth.append(wealthTop);
  var context=el('p','ws-chart-context');context.textContent=window.ACCT==='toss'?'토스 계좌 잔고 · 매매는 토스 앱 자동모으기 (조회 전용)':window.ACCT==='nh'?'VR 기수별 평가금과 밴드(라오어식) · 위 버튼으로 기수 선택':window.ACCT==='all'?'전체 계좌 합산 자산 · 아래 추이는 별도 시계열 기준':'일별 마지막 자산 기록 · 입출금 발생일 함께 표시';wealth.append(context);
  cards.cw1.classList.add('ws-wealth-chart');wealth.append(cards.cw1);
  var supporting=el('div','ws-supporting');[ks[3],ks[4],ks[5]].forEach(k=>supporting.append(k));wealth.append(supporting);main.append(wealth);
  var rail=el('aside','ws-rail');var state=el('section','ws-operation');
  var stateHead=el('div','ws-section-head');stateHead.append(el('h2','','운용 체크'),link('모니터링 ↗','mon'));state.append(stateHead);
  var live=el('div','ws-live');live.append(el('span','ws-live-dot'),el('span','',window.MET.automation&&window.MET.automation.running?'자동매매 시스템 가동 중':'자동매매 시스템 상태 확인'));if(!window.MET.automation||!window.MET.automation.running)live.classList.add('attention');state.append(live);
  ks[1].classList.add('ws-strategy-count');state.append(ks[1]);cards.slist.classList.add('ws-strategy-list');state.append(cards.slist);state.append(link('전략 설정 열기 →','strat','ws-wide-link'));rail.append(state);
  var actions=el('section','ws-next');actions.append(el('span','ws-overline','ORDER DESK'),el('h2','','주문 흐름 확인'),el('p','','예정 주문부터 미체결·체결 내역까지'),link('주문 / 체결 확인 →','order','ws-primary'));rail.append(actions);main.append(rail);shell.append(main);
  var alert=el('div','ws-alert-row');cards.palert.classList.add('ws-alert-card');alert.append(cards.palert);shell.append(alert);
  var activity=el('div','ws-activity');cards.phold.classList.add('ws-holdings');cards.ptr.classList.add('ws-trades');
  // Preserve original table cells and click handlers; only placement changes.
  activity.append(cards.phold,cards.ptr);shell.append(activity);
  var analysis=el('div','ws-analysis-grid');analysis.append(cards.lg2,cards.rbars);
  shell.append(openGroup('ws-analysis','전략별 상세 분석','손익 구성 · 수익률 비교 · 성과 요약',[analysis,cards.psum]));
  oldChildren.forEach(n=>{if(n.parentNode===page)n.remove();});page.append(shell);
 }
 function refresh(){var current=window.PAGE,same=current===previousPage;var y=typeof window.scrollY==='number'?window.scrollY:0;
  document.querySelectorAll('.ws-disclosure').forEach(d=>{disclosure[d.id]=d.open;});
  var result=originalRender.apply(this,arguments);enhance();previousPage=current;
  if(typeof requestAnimationFrame==='function')requestAnimationFrame(function(){
   // Resizing is presentation-only and preserves original dataset/options.
   [window.C1,window.C2,window.C3].forEach(c=>{if(c&&c.resize)c.resize();});
   if(same&&y>0)window.scrollTo(0,y);
  });return result;
 }
 window.render=refresh;
 window.tsSetWorkspace=function(value){enabled=!!value;var s=document.getElementById('ts-workspace-css');if(s)s.disabled=!enabled;refresh();};
 var mobile=el('nav','ws-mobile');mobile.setAttribute('aria-label','빠른 메뉴');
 [['dash','대시보드','fa-gauge-high'],['strat','전략','fa-sliders'],['order','주문','fa-receipt'],['menu','전체 메뉴','fa-bars']].forEach(a=>{
  var b=el('button');b.type='button';b.dataset.page=a[0];var icon=el('i','fa-solid '+a[2]);b.append(icon,el('span','',a[1]));b.onclick=function(){if(a[0]==='menu')document.getElementById('hamb').click();else window.go(a[0]);};mobile.append(b);
 });document.body.append(mobile);
 document.getElementById('hamb').setAttribute('aria-label','전체 메뉴 열기');document.getElementById('refresh').setAttribute('aria-label','새로고침');
 document.addEventListener('keydown',function(e){if(e.key==='Escape'){document.getElementById('sb').classList.remove('open');document.getElementById('scrim').classList.remove('show');}});
 if(window.MET)refresh();else document.body.classList.add('ws-v3');
}());</script>
<!-- TS_V3_FOOT_END -->
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    """통합 네이티브 SPA 셸 (iframe 미사용 · 검증된 전략 API 호출로 구성)."""
    from core.strategy_adapters import DISPLAY_NAMES
    import json as _json
    strategies = [
        {
            "key": k,
            "label": DISPLAY_NAMES.get(k, k),
            "sub": _STRAT_META.get(k, {}).get("sub", k),
            "icon": _STRAT_META.get(k, {}).get("icon", "fa-chart-line"),
            "kind": _STRAT_META.get(k, {}).get("kind", "ddsop"),
            "logic": _STRAT_META.get(k, {}).get("logic", ""),
        }
        for k in SUB_APPS
    ]
    html = _SHELL_HTML.replace("__STRATS__", _json.dumps(strategies, ensure_ascii=False))
    return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
