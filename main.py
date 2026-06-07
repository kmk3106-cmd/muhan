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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("trading_suite")

SUB_APPS = {"infinite": infinite_app, "ddsop": ddsop_app, "jongsa": jongsa_app}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        for name, sub in SUB_APPS.items():
            await stack.enter_async_context(sub.router.lifespan_context(sub))
            logger.info(f"[suite] sub-app lifespan 기동: {name}")
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


@app.get("/api/suite/metrics")
def suite_metrics():
    from core.suite_metrics import build_metrics
    return build_metrics()


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
    """실 현금 입출금 원장 (사용자 기록)."""
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
                 "※ KIS가 MOC 매수를 불허(매도전용)해 LOC로 종가매수. 각 트렌치는 매수평단 +목표%"
                 "(기본 3.5%) 도달 시 LOC 익절(목표가 보장), 40거래일 내 미도달 시 MOC 손절. "
                 "첫 트렌치 매도로 싸이클 종료. (떨사오팔과 매수 방식만 다름)"},
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
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/css/all.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--blue:#2f6bff;--blue-s:#eaf0ff;--indigo:#6c5ce7;--green:#16a34a;--green-s:#e8f6ec;
--red:#e5484d;--red-s:#fde8e8;--amber:#d97706;--amber-s:#fdf2e3;--bg:#f3f5f9;--card:#fff;
--line:#e9edf3;--c0:#1a2233;--c1:#5b6577;--c2:#9aa3b2;--c3:#cdd4df;--sb:212px;--hd:62px;
--sh:0 1px 2px rgba(20,28,46,.04),0 2px 8px rgba(20,28,46,.05);--rd:13px}
*{box-sizing:border-box;margin:0;padding:0}html,body{height:100%}
body{font-family:'Noto Sans KR',sans-serif;background:var(--bg);color:var(--c0);
-webkit-font-smoothing:antialiased;font-size:13px}
::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:var(--c3);
border-radius:8px;border:2px solid transparent;background-clip:content-box}
a{color:inherit;text-decoration:none}
.wrap{display:flex;min-height:100vh}
.sb{width:var(--sb);background:var(--card);border-right:1px solid var(--line);position:fixed;
inset:0 auto 0 0;display:flex;flex-direction:column;z-index:30}
.brand{display:flex;align-items:center;gap:10px;padding:17px 18px;border-bottom:1px solid var(--line)}
.brand .m{width:34px;height:34px;border-radius:9px;color:#fff;display:flex;align-items:center;
justify-content:center;font-size:15px;background:linear-gradient(135deg,var(--blue),var(--indigo))}
.brand b{font-size:14px;font-weight:800;letter-spacing:-.2px}
.brand small{display:block;font-size:9.5px;color:var(--c2);letter-spacing:.2em;margin-top:1px}
.nav{flex:1;overflow-y:auto;padding:10px}
.ni{display:flex;align-items:center;gap:12px;width:100%;text-align:left;border:none;
background:none;cursor:pointer;padding:11px 12px;border-radius:9px;color:var(--c1);
font-family:inherit;font-size:13px;font-weight:500;margin-bottom:2px;transition:.14s}
.ni:hover{background:var(--bg);color:var(--c0)}
.ni.on{background:var(--blue);color:#fff;font-weight:600;box-shadow:0 4px 12px rgba(47,107,255,.28)}
.ni .i{width:18px;text-align:center;font-size:14px;opacity:.7}.ni.on .i{opacity:1}
.ni .ch{margin-left:auto;font-size:10px;opacity:.4}
.sbhelp{margin:10px;padding:14px;border-radius:11px;background:var(--bg);font-size:11.5px;color:var(--c1)}
.sbhelp b{display:block;color:var(--c0);font-size:12px;margin-bottom:4px}
.mn{flex:1;margin-left:var(--sb);min-width:0;display:flex;flex-direction:column}
.hd{height:var(--hd);background:var(--card);border-bottom:1px solid var(--line);display:flex;
align-items:center;gap:16px;padding:0 24px;position:sticky;top:0;z-index:20}
.hamb{display:none;border:none;background:none;font-size:18px;color:var(--c1);cursor:pointer}
.hd .ttl{font-size:17px;font-weight:800;letter-spacing:-.3px}
.hd .sp{flex:1}
.hd .st{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600;color:var(--c1)}
.hd .st .d{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px var(--green-s)}
.hd .st.off .d{background:var(--red);box-shadow:0 0 0 3px var(--red-s)}
.hd .dt{font-size:12px;color:var(--c2);font-variant-numeric:tabular-nums}
.btn{font-family:inherit;padding:8px 14px;border-radius:9px;font-size:12.5px;font-weight:600;
cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--c1);
display:inline-flex;align-items:center;gap:7px;transition:.14s}
.btn:hover{background:var(--bg);color:var(--c0)}
.btn.p{background:var(--blue);color:#fff;border-color:var(--blue)}
.btn.p:hover{background:#2358e0;color:#fff}
.btn.sm{padding:5px 10px;font-size:11.5px}
.btn.dg{color:var(--red);border-color:var(--red-s)}.btn.dg:hover{background:var(--red-s)}
.body{padding:22px 24px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(176px,1fr));gap:14px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--rd);padding:16px 18px;
box-shadow:var(--sh);display:flex;align-items:flex-start;gap:12px}
.kpi .ic{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;
justify-content:center;font-size:14px;background:var(--blue-s);color:var(--blue);flex-shrink:0}
.kpi .ic.g{background:var(--green-s);color:var(--green)}.kpi .ic.r{background:var(--red-s);color:var(--red)}
.kpi .ic.n{background:var(--bg);color:var(--c2)}.kpi .ic.a{background:var(--amber-s);color:var(--amber)}
.kpi .lab{font-size:11.5px;color:var(--c1);font-weight:500}
.kpi .v{font-size:21px;font-weight:800;color:var(--c0);margin-top:7px;letter-spacing:-.5px;
font-variant-numeric:tabular-nums;line-height:1.1}
.kpi .v.up{color:var(--green)}.kpi .v.down{color:var(--red)}
.kpi .v small{font-size:12px;font-weight:600;color:var(--c2)}
.kpi .s{font-size:11px;margin-top:6px;color:var(--c2);font-variant-numeric:tabular-nums}
.up{color:var(--green);font-weight:700}.dn{color:var(--red);font-weight:700}
.grid{display:grid;gap:16px;margin-top:16px}
.g-3-1{grid-template-columns:2fr 1fr}.g-2{grid-template-columns:1fr 1fr}
@media(max-width:1080px){.g-3-1,.g-2{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--rd);box-shadow:var(--sh)}
.ch{display:flex;align-items:center;gap:10px;padding:15px 18px;border-bottom:1px solid var(--line)}
.ch .ct{font-size:13.5px;font-weight:700;flex:1}.ch .ct i{color:var(--c3);margin-right:7px}
.ch .lk{font-size:11.5px;color:var(--blue);font-weight:600;cursor:pointer}
.seg{display:flex;gap:2px;background:var(--bg);padding:3px;border-radius:8px}
.sgb{border:none;background:none;color:var(--c2);font-family:inherit;font-weight:600;font-size:11px;
padding:5px 10px;border-radius:6px;cursor:pointer}.sgb.on{background:#fff;color:var(--c0);box-shadow:var(--sh)}
.cw{position:relative;height:264px;padding:14px 16px}
.donut-w{position:relative;height:200px}.donut-c{position:absolute;inset:0;display:flex;
flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
.donut-c s{font-size:10.5px;color:var(--c2)}.donut-c b{font-size:17px;font-weight:800;margin-top:2px}
.lg{padding:6px 18px 16px;display:flex;flex-direction:column;gap:8px}
.lg .r{display:flex;align-items:center;gap:8px;font-size:12px}
.lg .r i{width:9px;height:9px;border-radius:3px;flex-shrink:0}.lg .r .n{flex:1;color:var(--c1)}
.lg .r .a{font-weight:700;font-variant-numeric:tabular-nums}.lg .r .p{color:var(--c2);width:46px;text-align:right}
.bars{padding:14px 18px;display:flex;flex-direction:column;gap:13px}
.bar{font-size:12px}.bar .t{display:flex;justify-content:space-between;margin-bottom:5px}
.bar .t b{font-weight:700;font-variant-numeric:tabular-nums}
.bar .tr{height:8px;background:var(--bg);border-radius:6px;overflow:hidden}
.bar .tr i{display:block;height:100%;border-radius:6px}
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th{text-align:left;color:var(--c2);font-weight:600;padding:11px 18px;
border-bottom:1px solid var(--line);font-size:10.5px;letter-spacing:.03em;text-transform:uppercase;background:#fbfcfe}
.tbl td{padding:12px 18px;border-bottom:1px solid var(--line);color:var(--c1);font-variant-numeric:tabular-nums}
.tbl tbody tr:hover{background:#fafbfe}.tbl tr:last-child td{border-bottom:0}.tbl td b{color:var(--c0)}
.dn8{display:inline-flex;align-items:center;gap:8px}.dn8 i{width:8px;height:8px;border-radius:50%}
.bdg{padding:4px 10px;border-radius:16px;font-size:11px;font-weight:700;display:inline-flex;align-items:center;gap:5px}
.bdg::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.bdg.run{background:var(--green-s);color:#15803d}.bdg.stop{background:var(--red-s);color:#c23030}
.bdg.part{background:var(--amber-s);color:#b45309}
.tag{padding:3px 9px;border-radius:6px;font-size:11px;font-weight:700}
.tag.buy{background:var(--red-s);color:#c23030}.tag.sell{background:var(--blue-s);color:var(--blue)}
.al{display:flex;gap:10px;padding:12px 18px;border-bottom:1px solid var(--line);font-size:12px}
.al:last-child{border-bottom:0}.al .ad{width:7px;height:7px;border-radius:50%;margin-top:5px;flex-shrink:0}
.al .ad.e{background:var(--red)}.al .ad.w{background:var(--amber)}.al .ad.i{background:var(--green)}
.al .am{flex:1;color:var(--c1);line-height:1.5;word-break:break-all}
.al .at{display:block;color:var(--c2);font-size:10.5px;margin-top:3px;font-variant-numeric:tabular-nums}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:200px;
color:var(--c2);text-align:center;gap:9px;padding:30px}.empty i{font-size:28px;color:var(--c3)}
.empty .t{font-size:13px;font-weight:600;color:var(--c1)}.empty .s{font-size:11.5px}
.muted{color:var(--c2);font-size:12.5px;padding:26px;text-align:center}
.form{padding:18px;display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:720px){.form{grid-template-columns:1fr}}
.fld label{display:block;font-size:11.5px;color:var(--c1);font-weight:600;margin-bottom:6px}
.fld input,.fld select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;
font-family:inherit;font-size:13px;color:var(--c0);background:#fff}
.fld input:focus,.fld select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-s)}
.fnote{grid-column:1/-1;font-size:11.5px;color:var(--c2)}
.fact{grid-column:1/-1;display:flex;gap:9px;justify-content:flex-end}
.tip{font-size:11.5px;color:var(--c1);background:var(--blue-s);padding:10px 14px;border-radius:9px;
margin:0 18px 16px;display:flex;gap:8px;align-items:flex-start}.tip i{color:var(--blue);margin-top:2px}
.hidden{display:none!important}
.scrim{display:none;position:fixed;inset:0;background:rgba(20,28,46,.45);z-index:25}
.toast{position:fixed;right:20px;bottom:20px;background:var(--c0);color:#fff;padding:13px 18px;
border-radius:10px;font-size:12.5px;z-index:50;box-shadow:0 8px 24px rgba(0,0,0,.2);opacity:0;
transform:translateY(8px);transition:.2s;pointer-events:none}.toast.s{opacity:1;transform:none}
@media(max-width:980px){.sb{transform:translateX(-100%);transition:.25s}.sb.open{transform:none}
.mn{margin-left:0}.hamb{display:block}.scrim.show{display:block}.body{padding:14px}}
.vtblk{padding:15px 18px;border-bottom:1px solid var(--line)}.vtblk:last-child{border-bottom:0}
.vth{display:flex;align-items:center;gap:10px;font-size:13px}
.trcells{display:flex;flex-wrap:wrap;gap:6px;padding:11px 0 10px}
.trc{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
border-radius:8px;background:var(--bg);color:var(--c2);font-size:11px;font-weight:700;
border:1px solid var(--line)}
.trc.on{background:var(--green-s);color:#15803d;border-color:#bfe6cc}
.vtblk details summary{cursor:pointer;font-size:11.5px;color:var(--blue);font-weight:600;
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
  .hd .ttl{font-size:15px;flex:0 1 auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
  /* 표는 가로 관성 스크롤 (래퍼가 없어도 표 자체에 적용) */
  .tbl{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;white-space:nowrap}
  .tbl thead,.tbl tbody,.tbl tr{display:table;width:100%;table-layout:fixed}
  .grid{margin-top:14px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
}

/* 휴대폰 — 600px 이하: 헤더 축약 · KPI 2열 · 차트 높이 보정 */
@media(max-width:600px){
  .hd{height:calc(54px + env(safe-area-inset-top));padding-left:max(14px,env(safe-area-inset-left));
  padding-right:max(14px,env(safe-area-inset-right))}
  .hd .ttl{font-size:14px}
  .hd .dt{display:none}                 /* 날짜시계 숨김(공간확보) */
  .hd .st #stt{display:none}            /* 연결상태 텍스트 숨김, 점만 유지 */
  .hd .st{gap:0}
  #refresh{font-size:0;padding:9px 11px;min-width:40px;justify-content:center} /* 아이콘만 */
  #refresh i{font-size:14px}
  .kpis{grid-template-columns:1fr 1fr;gap:10px}
  .kpi{padding:13px 13px;gap:10px}
  .kpi .ic{width:32px;height:32px;font-size:13px}
  .kpi .v{font-size:18px}
  .kpi .lab{font-size:11px}
  .cw{height:230px;padding:12px 8px}
  .donut-w{height:184px}
  .ch{padding:13px 14px}
  .ch .ct{font-size:12.5px}
  .tbl th,.tbl td{padding:10px 13px}
  .form{padding:15px;gap:12px}
  .fact{flex-direction:column-reverse}
  .fact .btn{width:100%;justify-content:center}
  /* iOS 입력 포커스 시 자동 줌 방지 — 폰트 ≥16px */
  .fld input,.fld select{font-size:16px;padding:12px 13px}
  .btn,.btn.sm,.sgb,.ni{font-size:13px}
}

/* 소형 폰 — 380px 이하: KPI 1열 */
@media(max-width:380px){
  .kpis{grid-template-columns:1fr}
  .hd .ttl{font-size:13px}
  .body{padding:11px 11px calc(16px + env(safe-area-inset-bottom))}
}

/* 가로 모드 낮은 높이 — 차트 압축 */
@media(max-height:480px) and (orientation:landscape){
  .cw{height:200px}.donut-w{height:170px}
}
</style></head><body>
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
var C1=null,C2=null,C3=null;var PAL=['#2f6bff','#16a34a','#6c5ce7','#d97706','#e5484d'];
var BARPAL=['#16a34a','#2f6bff','#e5484d','#d97706','#6c5ce7'];
var MENU=[['dash','대시보드','fa-gauge-high'],['strat','전략 관리','fa-sliders'],
['port','포트폴리오','fa-briefcase'],['order','주문/체결','fa-receipt'],
['risk','리스크 관리','fa-shield-halved'],['perf','성과 분석','fa-chart-line'],
['mon','모니터링','fa-desktop'],['sys','시스템 설정','fa-gear']];
var TT={dash:'대시보드',strat:'전략 관리',port:'포트폴리오',order:'주문/체결',
risk:'리스크 관리',perf:'성과 분석',mon:'모니터링',sys:'시스템 설정'};
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
function kpi(lab,ic,icc,v,vc,s){return '<div class="kpi"><div class="ic '+(icc||'')+'">'+
 '<i class="fa-solid '+ic+'"></i></div><div><div class="lab">'+lab+'</div>'+
 '<div class="v '+(vc||'')+'">'+v+'</div><div class="s">'+(s||'')+'</div></div></div>';}
/* ---------- 대시보드 ---------- */
function pgDash(){var a=MET.account||{},au=MET.automation||{},ss=MET.strategies||[];
 var stockE=(a.total_assets-a.cash>0)?(a.total_assets-a.cash):0;
 var h='<div class="kpis">'+
  kpi('총 자산','fa-coins','b',money(a.total_assets),'','순투입 '+money(a.net_invested))+
  kpi('전략 수','fa-layer-group','n',au.total+' 개','','운용중 '+au.active+' · 정지 '+(au.total-au.active))+
  kpi('계좌 수익률','fa-chart-pie',(a.total_return_pct>=0?'g':'r'),
   (a.total_return_pct==null?'—':(a.total_return_pct>=0?'+':'')+Number(a.total_return_pct).toFixed(2)+'%'),
   (a.total_return_pct>=0?'up':'down'),'누적손익 '+(a.total_pnl>=0?'+':'')+money(a.total_pnl))+
  kpi('실현손익','fa-circle-check',(a.realized_pnl>=0?'g':'r'),
   (a.realized_pnl>=0?'+':'')+money(a.realized_pnl),(a.realized_pnl>=0?'up':'down'),'완료 싸이클 누적')+
  kpi('현금 비중','fa-money-bill-wave','n',
   (a.cash_ratio==null?'—':Number(a.cash_ratio).toFixed(1)+'%'),'','현금 '+money(a.cash))+
  kpi('리스크 수준','fa-shield-halved','a',
   (a.mdd_pct==null?'수집중':(Math.abs(a.mdd_pct)<8?'양호':Math.abs(a.mdd_pct)<15?'보통':'주의')),'',
   'MDD '+(a.mdd_pct==null?'—':Number(a.mdd_pct).toFixed(2)+'%'))+'</div>';
 var seg='<div class="seg" id="sg">'+['1주','1개월','3M','6M','전체'].map(function(r){
  return '<button class="sgb'+(r===R1?' on':'')+'">'+r+'</button>';}).join('')+'</div>';
 h+='<div class="grid g-3-1">'+
  card('전체 자산추이','fa-chart-area','<div class="cw" id="cw1"><canvas id="c1"></canvas></div>',seg)+
  '<div class="card"><div class="ch"><span class="ct"><i class="fa-solid fa-list"></i>전략 리스트</span></div>'+
  '<div id="slist"></div></div></div>';
 h+='<div class="grid g-2">'+
  card('전략별 손익 기여도','fa-chart-pie','<div class="donut-w"><canvas id="c2"></canvas>'+
   '<div class="donut-c"><s>실현손익 합계</s><b id="dtot">—</b></div></div><div class="lg" id="lg2"></div>')+
  card('전략별 현재 누적수익률','fa-ranking-star','<div class="bars" id="rbars"></div>')+'</div>';
 h+='<div class="grid">'+card('전략별 성과 요약','fa-table-list',
  '<div id="psum" style="overflow-x:auto"></div>')+'</div>';
 h+='<div class="grid">'+card('보유 종목 (계좌 실시간 · 매입단가·현재가·평가수익률)','fa-wallet',
  '<div id="phold" style="overflow-x:auto"></div>',
  '<span class="lk" id="holdts" style="color:var(--c2);cursor:default"></span>')+'</div>';
 h+='<div class="grid g-2">'+card('최근 매매 내역','fa-receipt',
  '<div id="ptr" style="overflow-x:auto"></div>')+
  card('주문 상태 · 시스템 알림','fa-bell','<div id="palert"></div>')+'</div>';
 $('page').innerHTML=h;
 $('slist').innerHTML=ss.map(function(s,i){return '<div style="display:flex;align-items:center;'+
  'gap:9px;padding:13px 18px;border-bottom:1px solid var(--line);font-size:12.5px">'+
  '<i style="width:9px;height:9px;border-radius:50%;background:'+PAL[i%5]+'"></i>'+
  '<b style="flex:1">'+esc(s.display_name)+'</b>'+sP(s.return_pct)+
  '<span class="bdg '+(s.kill_switch?'stop':'run')+'" style="margin-left:8px">'+
  (s.kill_switch?'정지':'운용중')+'</span></div>';}).join('');
 renderSum(ss);renderTr(MET.recent_trades||[]);renderAlert(ss);drawDonut(ss);renderHold(MET.holdings||{});
 [].forEach.call($('sg').children,function(b){b.onclick=function(){
  [].forEach.call($('sg').children,function(x){x.classList.remove('on');});
  b.classList.add('on');R1=b.textContent;drawLine();};});
 if(!SER){fetch('/api/suite/series').then(function(r){return r.json();}).then(function(d){
  SER=d;drawLine();});}else drawLine();
 renderBars(ss);}
function renderBars(ss){var mx=Math.max(1,Math.max.apply(null,ss.map(function(s){
  return Math.abs(s.return_pct||0);})));
 $('rbars').innerHTML=ss.map(function(s,i){var v=s.return_pct||0;
  return '<div class="bar"><div class="t"><span>'+esc(s.display_name)+'</span>'+sP(v)+
  '</div><div class="tr"><i style="width:'+(Math.abs(v)/mx*100)+'%;background:'+
  BARPAL[i%BARPAL.length]+'"></i></div></div>';}).join('');}
function renderSum(ss){$('psum').innerHTML='<table class="tbl"><thead><tr><th>전략명</th>'+
 '<th style="text-align:right">원금</th><th style="text-align:right">누적손익</th>'+
 '<th style="text-align:right">수익률</th><th style="text-align:right">승률</th>'+
 '<th style="text-align:right">보유</th><th>상태</th></tr></thead><tbody>'+
 ss.map(function(s,i){return '<tr><td><span class="dn8"><i style="background:'+PAL[i%5]+
 '"></i><b>'+esc(s.display_name)+'</b></span></td><td style="text-align:right">'+money(s.invested)+
 '</td><td style="text-align:right">'+sM(s.realized_pnl)+'</td><td style="text-align:right">'+
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
function renderHold(h){var its=(h&&h.items)||[];var box=$('phold');var tsEl=$('holdts');
 if(tsEl)tsEl.textContent=h&&h.ts?('갱신 '+String(h.ts).replace('T',' ').slice(0,16)):'';
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
   (x.name?'<br><span style="color:var(--c2);font-size:10.5px">'+esc(x.name)+'</span>':'')+'</td>'+
   '<td>'+esc(x.display_name||'-')+'</td>'+
   '<td style="text-align:right">'+x.qty+'</td>'+
   '<td style="text-align:right">'+money(x.avg_price,2)+'</td>'+
   '<td style="text-align:right">'+money(x.now_price,2)+'</td>'+
   '<td style="text-align:right">'+money(x.buy_amt,2)+'</td>'+
   '<td style="text-align:right">'+money(x.eval_amt,2)+'</td>'+
   '<td style="text-align:right" class="'+(up?'up':'dn')+'">'+(up?'+':'')+money(x.pnl,2)+'</td>'+
   '<td style="text-align:right" class="'+(up?'up':'dn')+'">'+(up?'+':'')+Number(x.pnl_rt||0).toFixed(2)+'%</td></tr>';}).join('')+
  '</tbody></table>'+
  '<div style="padding:10px 18px;font-size:12px;color:var(--c1);border-top:1px solid var(--line)">'+
  '합계 매입 <b>'+money(tBuy)+'</b> · 평가 <b>'+money(tEval)+'</b> · 평가손익 <b class="'+(tPnl>=0?'up':'dn')+'">'+
  (tPnl>=0?'+':'')+money(tPnl)+'</b> · 수익률 <b class="'+(tRt>=0?'up':'dn')+'">'+(tRt>=0?'+':'')+tRt.toFixed(2)+'%</b></div>';}
function renderAlert(ss){var rows=[];ss.forEach(function(s){(s.errors||[]).forEach(function(l){
 rows.push({lv:l.level,m:'['+s.display_name+'] '+l.message,t:l.created_at});});});
 rows.sort(function(a,b){return (b.t||'').localeCompare(a.t||'');});
 $('palert').innerHTML=rows.length?rows.slice(0,7).map(function(r){
 var c=r.lv==='ERROR'?'e':(r.lv==='WARNING'?'w':'i');
 return '<div class="al"><span class="ad '+c+'"></span><span class="am">'+esc(r.m)+
 '<span class="at">'+esc((r.t||'').replace('T',' ').slice(0,19))+'</span></span></div>';}).join(''):
 '<div class="al"><span class="ad i"></span><span class="am">자동매매 정상 운영 중 · 최근 오류 없음</span></div>';}
function drawDonut(ss){if(C2){C2.destroy();C2=null;}var L=[],V=[],T=0;
 ss.forEach(function(s){var v=Math.abs(s.realized_pnl||0);if(s.realized_pnl){L.push(s.display_name);
  V.push(s.realized_pnl);T+=(s.realized_pnl||0);}});
 $('dtot').innerHTML=(T>=0?'+':'')+money(T);
 if(!L.length){$('lg2').innerHTML='<div class="muted">실현손익 데이터 없음</div>';return;}
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
function drawLine(){var w=$('cw1');if(!SER||SER.collecting||!SER.points||SER.points.length<2){
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
 var EST=dp.map(function(o){return !!o.x.est;});
 var hasEst=EST.indexOf(true)>=0;
 var ds=[{label:'총자산'+(hasEst?' (점선=추정 소급)':''),
  data:dp.map(function(o){return o.x.total_assets;}),borderColor:'#2f6bff',
  backgroundColor:'rgba(47,107,255,.08)',borderWidth:2.4,pointRadius:2,fill:true,tension:.2,yAxisID:'y',
  segment:{borderDash:function(c){return (EST[c.p0DataIndex]||EST[c.p1DataIndex])?[5,4]:undefined;}}}];
 if(dp.some(function(o){return o.x.deposit!=null;})){
  ds.push({label:'입금액(누적)',data:dp.map(function(o){return o.x.deposit;}),
   borderColor:'#16a34a',borderWidth:2,pointRadius:0,tension:.1,stepped:true,yAxisID:'y'});}
 if(dp.some(function(o){return o.x.withdraw!=null;})){
  ds.push({label:'출금액(누적)',data:dp.map(function(o){return o.x.withdraw;}),
   borderColor:'#e5484d',borderWidth:2,pointRadius:0,tension:.1,stepped:true,yAxisID:'y'});}
 C1=new Chart($('c1'),{type:'line',data:{labels:L,datasets:ds},options:{responsive:true,
  maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
  plugins:{legend:{position:'bottom',labels:{usePointStyle:true,pointStyle:'circle',boxWidth:7,
   padding:14,font:{size:11}}},tooltip:{backgroundColor:'#1a2233',padding:10,cornerRadius:8}},
  scales:{x:{grid:{display:false},title:{display:true,text:'일자',color:'#9aa3b2',font:{size:10}},
   ticks:{color:'#9aa3b2',font:{size:10},maxTicksLimit:10}},
  y:{position:'left',grid:{color:'#eef1f6'},ticks:{color:'#9aa3b2',font:{size:10},
   callback:function(v){return '$'+(v/1000).toFixed(0)+'k';}}}}}});}
/* ---------- 전략 관리 ---------- */
function pgStrat(){var ss=MET.strategies||[];
 var opt=STRATS.map(function(s){return '<option value="'+s.key+'">'+esc(s.label)+'</option>';}).join('');
 var h='<div class="grid g-2"><div class="card">'+
  '<div class="ch"><span class="ct"><i class="fa-solid fa-list-check"></i>전략 선택 · 시드 배분</span></div>'+
  '<div class="form"><div class="fld"><label>전략</label><select id="sSel">'+opt+'</select></div>'+
  '<div class="fld"><label>전략 시드 할당 총액 (USD)</label><input id="sBud" type="number" placeholder="예: 10000"></div>'+
  '<div class="fnote" id="sBinfo">—</div>'+
  '<div class="tip" style="grid-column:1/-1;margin:0" id="sLogic">'+
  '<i class="fa-solid fa-circle-info"></i><span>전략 로직</span></div>'+
  '<div class="fact"><button class="btn p" onclick="saveBudget()">시드 할당 저장</button></div></div></div>'+
  '<div class="card"><div class="ch"><span class="ct"><i class="fa-solid fa-plus"></i>티커 추가</span></div>'+
  '<div id="addForm"></div></div></div>';
 h+='<div class="grid">'+card('선택 전략 종목 목록','fa-coins','<div id="tklist" style="overflow-x:auto"></div>',
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
  $('sBud').value=b.assigned_total!=null?b.assigned_total:'';
  $('sBinfo').innerHTML='현재 사용 <b>'+money(b.used)+'</b> / 할당 <b>'+
   (b.assigned_total==null?'미설정':money(b.assigned_total))+'</b> · 종목 '+(b.ticker_count||0)+
   (b.over_budget?' · <span class="dn">예산 초과</span>':'');});
 var kind=kindOf(k);
 var jong=(kind==='jongsa');
 // 트렌치형(떨사오팔/종사종팔): x(%) 라벨·기본값·안내문만 전략별로 다름. API/필드ID는 동일(Ticker API).
 var xLabel=jong?'목표 수익률 (%)':'x (%)';
 var xDefault=jong?'3.5':'3';
 var ntDefault=jong?'7':'5';   // 종사종팔 기본 7트렌치 (사용자 변경 가능)
 var trNote=jong
  ?'종사종팔: 매 거래일 다음 트렌치를 <b>종가 LOC 매수(한도 전일종가+15%)</b>, 매수평단 +목표% 도달 시 LOC 익절, 40거래일 손절. 티커는 전 전략 통틀어 중복 불가.'
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
   '<button class="btn sm" onclick="togTrade(\''+k+'\','+r.id+')">진행 토글</button> '+
   '<button class="btn sm dg" onclick="delTicker(\''+k+'\','+r.id+')">삭제</button></td></tr>';
  }).join('')+'</tbody></table>';});}
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
function togTrade(k,id){var kind=kindOf(k);
 api('PATCH','/'+k+'/api/'+(kind==='infinite'?'portfolios':'tickers')+'/'+id+'/trading')
  .then(function(){toast('진행 상태 변경');loadStratMgr();}).catch(function(e){toast('실패: '+e);});}
function delTicker(k,id){if(!confirm('이 종목을 삭제할까요? (성공리포트는 보존)'))return;
 var kind=kindOf(k);
 api('DELETE','/'+k+'/api/'+(kind==='infinite'?'portfolios':'tickers')+'/'+id)
  .then(function(){toast('삭제됨');loadStratMgr();}).catch(function(e){toast('실패: '+e);});}
/* ---------- 포트폴리오 ---------- */
function pgPort(){var ss=MET.strategies||[];var rows=[];
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
  '<span style="font-size:11.5px;color:var(--c2)">매수=초록 · 대기=회색</span>')+'</div>';}
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
  '<span style="color:var(--c2);font-size:11.5px">'+esc(strat)+'</span>'+
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
   '<span style="color:var(--c3);font-size:10px">'+arrow(k)+'</span></th>';};
 var stratOpts='<option value="">전략 전체</option>'+STRATS.map(function(s){
   return '<option value="'+s.key+'"'+(TRDF.s===s.key?' selected':'')+'>'+esc(s.label)+'</option>';}).join('');
 var ipStyle='padding:7px 10px;border:1px solid var(--line);border-radius:8px;'+
   'font-family:inherit;font-size:12.5px;background:#fff;color:var(--c0)';
 var bar='<div style="padding:12px 16px;border-bottom:1px solid var(--line);'+
   'display:flex;flex-wrap:wrap;gap:8px;align-items:center">'+
   '<select onchange="TRDF.s=this.value;renderTradesTable();" style="'+ipStyle+'">'+stratOpts+'</select>'+
   '<input placeholder="티커" value="'+esc(TRDF.t||'')+'" '+
   'oninput="TRDF.t=this.value;renderTradesTable();" style="'+ipStyle+';width:100px;text-transform:uppercase">'+
   '<input type="date" value="'+_ymd2iso(TRDF.d1)+'" '+
   'onchange="TRDF.d1=this.value.replace(/-/g,\'\');renderTradesTable();" style="'+ipStyle+'">'+
   '<span style="color:var(--c2);font-size:11.5px">~</span>'+
   '<input type="date" value="'+_ymd2iso(TRDF.d2)+'" '+
   'onchange="TRDF.d2=this.value.replace(/-/g,\'\');renderTradesTable();" style="'+ipStyle+'">'+
   '<button class="btn sm" onclick="trdReset();"><i class="fa-solid fa-rotate-left"></i> 초기화</button>'+
   '<span style="color:var(--c2);font-size:11.5px;margin-left:auto">'+
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
 '<div class="card"><div class="ch"><span class="ct"><i class="fa-solid fa-receipt"></i>주문/체결</span>'+
 '<div class="seg" id="og"><button class="sgb on">오늘 주문(예정)</button>'+
 '<button class="sgb">미체결</button><button class="sgb">체결 내역</button></div></div>'+
 '<div class="tip"><i class="fa-solid fa-robot"></i>모든 주문은 스케줄 워커가 100% 자동 생성·제출합니다. '+
 '아래는 조회 전용입니다(수동 주문 없음).</div><div id="otab"><div class="muted">불러오는 중…</div></div></div></div>';
 var tabs=$('og').children;[].forEach.call(tabs,function(b,i){b.onclick=function(){
  [].forEach.call(tabs,function(x){x.classList.remove('on');});b.classList.add('on');oTab(i);};});
 oTab(0);}
function oTab(i){var box=$('otab');box.innerHTML='<div class="muted">불러오는 중…</div>';
 var paths=STRATS.map(function(s){return s.key;});
 if(i===0){Promise.all(paths.map(function(k){return fetch('/'+k+'/api/orders/today')
  .then(function(r){return r.json();}).then(function(d){return (d||[]).map(function(o){
   o._s=labelOf(k);return o;});}).catch(function(){return [];});})).then(function(rs){
  var all=[].concat.apply([],rs);box.innerHTML=all.length?('<table class="tbl"><thead><tr>'+
  '<th>전략</th><th>티커</th><th>구분</th><th>유형</th><th style="text-align:right">수량</th>'+
  '<th style="text-align:right">가격</th><th style="text-align:right">금액</th><th>설명</th></tr></thead><tbody>'+
  all.map(function(o){return '<tr><td>'+esc(o._s)+'</td><td><b>'+esc(o.ticker)+'</b></td>'+
  '<td><span class="tag '+(o.side==='buy'?'buy">매수':'sell">매도')+'</span></td><td>'+esc(o.order_type||'')+
  '</td><td style="text-align:right">'+o.qty+'</td><td style="text-align:right">'+money(o.price,2)+
  '</td><td style="text-align:right">'+money(o.amount,2)+'</td><td style="color:#9aa3b2;font-size:11px">'+
  esc(o.desc||'')+'</td></tr>';}).join('')+'</tbody></table>'):
  '<div class="muted">오늘 예정 주문 없음 (장 시작 전이거나 조건 미충족)</div>';});}
 else if(i===1){Promise.all(paths.map(function(k){return fetch('/'+k+'/api/orders/pending')
  .then(function(r){return r.json();}).then(function(d){return ((d&&d.items)||[]).map(function(o){
   o._s=labelOf(k);return o;});}).catch(function(){return [];});})).then(function(rs){
  var all=[].concat.apply([],rs);box.innerHTML=all.length?('<table class="tbl"><thead><tr>'+
  '<th>전략</th><th>티커</th><th>구분</th><th style="text-align:right">수량</th>'+
  '<th style="text-align:right">가격</th><th>주문번호</th><th>주문시각</th></tr></thead><tbody>'+
  all.map(function(o){return '<tr><td>'+esc(o._s)+'</td><td><b>'+esc(o.ticker)+'</b></td><td>'+
  esc(o.side_label||o.side||'')+'</td><td style="text-align:right">'+o.qty+
  '</td><td style="text-align:right">'+money(o.price,2)+'</td><td>'+esc(o.order_no||'')+'</td><td>'+
  esc((o.ord_dt||'')+' '+(o.ord_tmd||''))+'</td></tr>';}).join('')+'</tbody></table>'):
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
 var h='<div class="tip"><i class="fa-solid fa-rotate"></i>무한매수법 V2.2·떨사오팔 모두 '+
  '<b>싸이클 방식</b>입니다. 전략·종목별 싸이클 손익과 해당 싸이클의 매수/매도 거래를 확인하세요. '+
  '진행중 싸이클은 종료 시 집계됩니다.</div>'+
  '<div class="grid">'+card('전략 실현 성과 요약','fa-chart-line',
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
   '<b style="font-size:13px">'+esc(title)+' 매수/매도 상세</b> '+
   '<span style="color:var(--c2);font-size:11.5px">매수 '+bs.length+'건 '+money(bSum)+
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
  '<i class="fa-solid fa-building-columns"></i>계좌 · 시스템 정보</span></div>'+
  '<div style="padding:4px 0">'+
  row('공용 계좌','69567573 (실계좌 · real)')+row('총 평가자산',money(a.total_assets))+
  row('예수금',money(a.cash))+row('스냅샷 시각',esc((a.snapshot_at||'').replace('T',' ').slice(0,16)))+
  row('운용 전략',MET.automation.active+' / '+MET.automation.total)+'</div></div>'+
  '<div class="card"><div class="ch"><span class="ct"><i class="fa-solid fa-power-off"></i>'+
  '전략 가동/정지 (Kill Switch)</span></div><div style="padding:4px 0">'+
  ss.map(function(s){return '<div style="display:flex;align-items:center;gap:10px;padding:13px 18px;'+
   'border-bottom:1px solid var(--line)"><b style="flex:1">'+esc(s.display_name)+'</b>'+
   '<span class="bdg '+(s.kill_switch?'stop':'run')+'">'+(s.kill_switch?'정지':'운용중')+'</span>'+
   '<button class="btn sm '+(s.kill_switch?'p':'dg')+'" onclick="togKill(\''+s.strategy+'\','+
   (s.kill_switch?'false':'true')+')">'+(s.kill_switch?'재가동':'정지')+'</button></div>';}).join('')+
  '</div></div></div>'+
  '<div class="card" style="margin-top:16px"><div class="ch"><span class="ct">'+
  '<i class="fa-solid fa-money-bill-transfer"></i>실 현금 입출금 원장</span>'+
  '<span style="font-size:11.5px;color:var(--c2)">차트 입금액/출금액 = 이 기록의 일자별 누적</span></div>'+
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
  '<i class="fa-solid fa-magnifying-glass-chart"></i>감시 기능 · 일일 정합 감사</span>'+
  '<span style="font-size:11.5px;color:var(--c2)">매일 09:00 KST 자동 · T값 동기화 + 싸이클 이력 정합</span>'+
  '<button class="btn sm p" style="margin-left:auto" onclick="tAuditRun()">'+
  '<i class="fa-solid fa-play"></i> 지금 검증</button></div>'+
  '<div id="tAuditBox"><div class="muted">불러오는 중…</div></div></div>'+
  '<div class="tip"><i class="fa-solid fa-shield-halved"></i>보안상 KIS 자격증명(앱키·시크릿) 입력은 '+
  '이 화면에서 다루지 않습니다. 실 입출금은 외부 거래라 거래데이터로 산출 불가 — 직접 기록해야 정확합니다.</div>';
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
 if(it.T_stored!=null){det='<div style="font-size:11px;color:var(--c2);margin-top:4px">'+
  'T(DB)='+it.T_stored+' · T(cum)='+it.T_recalc_cum+' · T(보유원가)='+it.T_from_holding+
  ' · cum_buy='+money(it.cum_buy)+' · cum_sell='+money(it.cum_sell)+
  ' · avg×qty='+money((it.avg_price||0)*(it.qty||0))+
  ' · B='+money(it.B,2)+' (seed '+money(it.seed)+' / A '+it.A+')</div>';}
 var reason=it.reason?('<div style="font-size:11.5px;color:var(--red);margin-top:5px;'+
  'background:var(--red-s);padding:8px 10px;border-radius:8px">'+esc(it.reason)+'</div>'):'';
 var note=it.note?('<div style="font-size:11px;color:var(--c2);margin-top:5px;'+
  'background:var(--bg);padding:7px 10px;border-radius:8px"><i class="fa-solid fa-circle-info"></i> '+
  esc(it.note)+'</div>'):'';
 return '<div style="padding:12px 18px;border-bottom:1px solid var(--line)">'+
  '<div style="display:flex;align-items:center;gap:10px">'+
  '<b style="font-size:13px">'+esc(it.ticker||'-')+'</b>'+
  '<span style="color:var(--c2);font-size:11px">싸이클 C'+(it.cycle||1)+'</span>'+
  '<span class="bdg '+b[0]+'" style="margin-left:auto">'+b[1]+'</span></div>'+
  det+reason+note+'</div>';}
function _renderCycItem(it){var b=_bdgFor(it.status);
 var scope=it.scope==='continuity'?' · 연속성':'';
 var det='';
 if(it.start_date){det='<div style="font-size:11px;color:var(--c2);margin-top:4px;word-break:break-all">'+
  esc(it.start_date)+' ~ '+esc(it.end_date)+' · trades '+(it.trades_count||0)+
  '건 (매수 '+(it.buys_count||0)+'/'+(it.buy_qty||0)+'주 '+money(it.buy_sum)+
  ' · 매도 '+(it.sells_count||0)+'/'+(it.sell_qty||0)+'주 '+money(it.sell_sum)+
  ') · history(buy '+money(it.history_buy)+' / sell '+money(it.history_sell)+
  ' / profit '+money(it.history_profit)+') · last_sell '+esc(it.last_sell_date||'-')+'</div>';}
 var reason=it.reason?('<div style="font-size:11.5px;color:var(--red);margin-top:5px;'+
  'background:var(--red-s);padding:8px 10px;border-radius:8px">'+esc(it.reason)+'</div>'):'';
 return '<div style="padding:12px 18px;border-bottom:1px solid var(--line)">'+
  '<div style="display:flex;align-items:center;gap:10px">'+
  '<b style="font-size:13px">'+esc(it.ticker||'-')+'</b>'+
  '<span style="color:var(--c2);font-size:11px">'+esc(it.strategy||'')+
  ' · C'+(it.cycle||'-')+scope+'</span>'+
  '<span class="bdg '+b[0]+'" style="margin-left:auto">'+b[1]+'</span></div>'+
  det+reason+'</div>';}
function renderTAudit(d){var box=$('tAuditBox');
 var ts=(d&&d.ts)||'';var overall=(d&&d.overall)||'none';
 if(!ts){box.innerHTML='<div class="muted">아직 감사 기록이 없습니다. "지금 검증"으로 1회 실행하세요.</div>';return;}
 var b=_bdgFor(overall);
 var head='<div style="padding:11px 18px;border-bottom:1px solid var(--line);'+
  'display:flex;align-items:center;gap:10px;font-size:12px;flex-wrap:wrap">'+
  '<span class="bdg '+b[0]+'">전체 '+b[1]+'</span>'+
  '<span style="color:var(--c2)">최근 검증 '+esc(ts.replace('T',' ').slice(0,19))+'</span>'+
  '<button class="btn sm" style="margin-left:auto" onclick="tAuditHistory()">'+
  '<i class="fa-solid fa-clock-rotate-left"></i> 이력 보기</button></div>';
 var sec=(d&&d.sections)||null;
 if(!sec){var items=(d&&d.items)||[];
  box.innerHTML=head+(items.length?items.map(_renderTItem).join(''):
   '<div class="muted">대상 포트폴리오 없음</div>');return;}
 var tv=sec.t_value||{items:[]};var ci=sec.cycle_integrity||{items:[]};
 var hdr=function(title,icon,sec,empty){
  return '<div style="padding:10px 18px;background:var(--bg);'+
   'display:flex;align-items:center;gap:9px;font-size:12px;font-weight:700;color:var(--c1);'+
   'border-bottom:1px solid var(--line);border-top:1px solid var(--line)">'+
   '<i class="fa-solid '+icon+'" style="color:var(--c2)"></i>'+esc(title)+
   _sectionOverallBadge(sec,empty)+'</div>';};
 var tvHtml=hdr('T값 동기화 (state.T vs 보유원가/B)','fa-equals',tv,'대상 없음')+
  ((tv.items||[]).length?tv.items.map(_renderTItem).join(''):
   '<div class="muted">대상 포트폴리오 없음</div>');
 var ciHtml=hdr('싸이클 이력 정합 (end_date · net qty · 금액 · profit)','fa-list-check',ci,'대상 없음')+
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
  var head='<div style="padding:10px 18px;font-size:12px;color:var(--c1)">총 입금 <b class="up">'+
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
 'border-bottom:1px solid var(--line);font-size:12.5px"><span style="color:var(--c1)">'+k+
 '</span><b>'+v+'</b></div>';}
function togKill(k,act){if(!confirm(act?'이 전략을 정지(Kill Switch ON)할까요?':'이 전략을 재가동할까요?'))return;
 api('POST','/'+k+'/api/kill_switch?activate='+act).then(function(){
  toast('상태 변경됨');loadAll();}).catch(function(e){toast('실패: '+e);});}
/* ---------- 라우터 ---------- */
function render(){if(!MET){$('page').innerHTML='<div class="muted">불러오는 중…</div>';return;}
 ({dash:pgDash,strat:pgStrat,port:pgPort,order:pgOrder,risk:pgRisk,perf:pgPerf,
   mon:pgMon,sys:pgSys}[PAGE]||pgDash)();}
function loadAll(){return fetch('/api/suite/metrics').then(function(r){return r.json();})
 .then(function(d){MET=d;var au=d.automation||{};
  $('st').className='st'+(au.running?'':' off');
  $('stt').textContent=au.running?'정상 운영중':'정지 상태';
  $('sbst').innerHTML='가동 '+au.active+'/'+au.total+' 전략<br>갱신 '+
   esc((d.generated_at||'').replace('T',' ').slice(11,19));
  render();}).catch(function(){$('page').innerHTML='<div class="muted">데이터 로드 실패</div>';});}
function tick(){$('dt').textContent=new Date().toLocaleString('ko-KR',{hour12:false});}
$('hamb').onclick=function(){$('sb').classList.toggle('open');$('scrim').classList.toggle('show');};
$('scrim').onclick=function(){$('sb').classList.remove('open');$('scrim').classList.remove('show');};
$('refresh').onclick=function(){SER=null;loadAll();};
buildNav();tick();setInterval(tick,1000);loadAll();
setInterval(function(){if(PAGE==='dash'||PAGE==='mon')loadAll();},60000);
</script></body></html>"""


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
