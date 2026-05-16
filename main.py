# -*- coding: utf-8 -*-
"""trading_suite 부모 FastAPI - 두 전략 sub-app 마운트 + lifespan 통합.

단일 프로세스·단일 포트(8000)에서 무한매수법/떨사오팔을 함께 운용한다.
각 전략의 스케줄러·웹소켓은 sub-app 자체 lifespan에서 기동되는데,
Starlette는 마운트된 sub-app의 lifespan을 자동 실행하지 않으므로
부모 lifespan에서 각 sub-app lifespan을 수동으로 진입/정리한다.
"""
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from strategies.infinite.main import app as infinite_app
from strategies.ddsop.main import app as ddsop_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("trading_suite")

# 마운트 경로 → sub-app (데이터 주도 · 신규 전략은 여기 + _STRAT_META 추가)
SUB_APPS = {"infinite": infinite_app, "ddsop": ddsop_app}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        for name, sub in SUB_APPS.items():
            await stack.enter_async_context(sub.router.lifespan_context(sub))
            logger.info(f"[suite] sub-app lifespan 기동: {name}")
        # equity 스냅샷터: DB만 읽음(KIS 미호출) → 레인 무관. 30분 주기 + 기동 직후 2포인트
        sched = None
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from core.equity_snapshot import snapshot as _equity_snapshot
            sched = BackgroundScheduler(timezone="Asia/Seoul")
            sched.add_job(_equity_snapshot, "interval", minutes=30,
                          id="equity_snapshot", max_instances=1)
            sched.add_job(_equity_snapshot, "date",
                          run_date=datetime.now() + timedelta(seconds=12),
                          id="equity_snapshot_b1")
            sched.add_job(_equity_snapshot, "date",
                          run_date=datetime.now() + timedelta(seconds=75),
                          id="equity_snapshot_b2")
            sched.start()
            logger.info("[suite] equity 스냅샷터 시작 (30분 주기, DB-only)")
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


class BudgetBody(BaseModel):
    total_usd: float


@app.get("/api/suite/strategies")
def suite_strategies():
    """전략별 활성 티커 + 시드 예산 가드레일."""
    from core.ticker_registry import all_active
    from core.strategy_budget import summary
    return {"active_tickers": all_active(), "budgets": summary()}


@app.get("/api/suite/metrics")
def suite_metrics():
    """통합 대시보드 지표 (읽기 전용 집계, 신규 KIS 호출 없음)."""
    from core.suite_metrics import build_metrics
    return build_metrics()


@app.get("/api/suite/series")
def suite_series():
    """차트용 시계열 (전략별 누적수익률). equity 스냅샷 누적분."""
    from core.equity_snapshot import series
    return series()


@app.post("/api/suite/strategies/{name}/budget")
def set_strategy_budget(name: str, body: BudgetBody):
    """전략별 시드 할당 총액 설정."""
    from core.strategy_budget import set_assigned_total
    try:
        set_assigned_total(name, body.total_usd)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"strategy": name, "assigned_total": body.total_usd}


# 전략 표시 메타 (데이터 주도 · 신규 전략은 SUB_APPS + 여기 항목만 추가)
_STRAT_META = {
    "infinite": {"sub": "무한매수법 v2.2 · 40분할", "icon": "fa-infinity"},
    "ddsop": {"sub": "떨사오팔 · n트렌치", "icon": "fa-droplet"},
    "jongsajongpal": {"sub": "종사종팔 · n트렌치", "icon": "fa-clock-rotate-left"},
    "infinite_v3": {"sub": "무한매수법 v3.0", "icon": "fa-infinity"},
}

_SHELL_HTML = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>trading_suite · 자동매매 대시보드</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/css/all.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--ink:#0f1b3d;--ink2:#16224a;--blue:#3b6ef5;--blue-soft:#eaf0ff;--indigo:#6c5ce7;
--green:#10b981;--green-soft:#e6f7f0;--red:#ef4444;--red-soft:#fdeced;--amber:#f59e0b;
--amber-soft:#fdf3e3;--bg:#f4f6fa;--line:#e9ecf3;--c0:#0f172a;--c1:#475569;--c2:#94a3b8;
--c3:#cbd5e1;--sb:215px;--hd:60px;--sh:0 1px 2px rgba(15,23,42,.04),0 2px 8px rgba(15,23,42,.05);
--sh2:0 6px 24px rgba(15,23,42,.08);--rd:14px}
*{box-sizing:border-box;margin:0;padding:0}html,body{height:100%}
body{font-family:'Noto Sans KR',sans-serif;background:var(--bg);color:var(--c0);
-webkit-font-smoothing:antialiased;font-size:13px}
::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:var(--c3);
border-radius:8px;border:2px solid transparent;background-clip:content-box}
.wrap{display:flex;min-height:100vh}
/* sidebar */
.sb{width:var(--sb);background:linear-gradient(180deg,var(--ink),var(--ink2));color:#fff;
position:fixed;inset:0 auto 0 0;display:flex;flex-direction:column;z-index:30}
.brand{display:flex;align-items:center;gap:11px;padding:18px 20px 20px}
.brand .m{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;
justify-content:center;font-size:17px;background:linear-gradient(135deg,var(--blue),var(--indigo));
box-shadow:0 6px 16px rgba(59,110,245,.4)}
.brand b{font-size:16px;font-weight:800;letter-spacing:.5px}
.brand small{display:block;font-size:10px;color:#8ea0c8;letter-spacing:.22em;margin-top:2px}
.nav{flex:1;overflow-y:auto;padding:6px 12px}
.nav::-webkit-scrollbar-thumb{background:rgba(255,255,255,.14)}
.ng{padding:16px 10px 7px;font-size:10.5px;font-weight:600;color:#7b8cb5;letter-spacing:.05em}
.ni{display:flex;align-items:center;gap:11px;width:100%;text-align:left;border:none;
background:none;cursor:pointer;padding:10px 12px;border-radius:9px;color:#b9c4e0;
font-family:inherit;font-size:13px;margin-bottom:2px;transition:.15s}
.ni:hover{background:rgba(255,255,255,.06);color:#fff}
.ni.on{background:linear-gradient(135deg,var(--blue),#4f7ff7);color:#fff;
box-shadow:0 6px 16px rgba(59,110,245,.35)}
.ni .i{width:18px;text-align:center;font-size:14px;opacity:.85}
.ni.on .i{opacity:1}.ni .t{flex:1;min-width:0}
.ni .t b{display:block;font-weight:600}.ni .t span{font-size:10.5px;color:#8294bd}
.ni.on .t span{color:rgba(255,255,255,.7)}
.ni .d{width:7px;height:7px;border-radius:50%;background:#56607f}
.ni .d.on{background:var(--green);box-shadow:0 0 0 3px rgba(16,185,129,.22)}
.ni .d.off{background:var(--red);box-shadow:0 0 0 3px rgba(239,68,68,.22)}
.sbcard{margin:10px 14px;padding:15px;border-radius:12px;
background:linear-gradient(135deg,rgba(59,110,245,.22),rgba(108,92,231,.18));
border:1px solid rgba(255,255,255,.08)}
.sbcard b{font-size:12.5px;font-weight:700}.sbcard p{font-size:11px;color:#9fb0d6;margin:5px 0 0;line-height:1.6}
.sbfoot{padding:14px 20px;border-top:1px solid rgba(255,255,255,.07);font-size:11px;
color:#8294bd;line-height:1.7}.sbfoot b{color:#cdd7ee}
/* main */
.mn{flex:1;margin-left:var(--sb);min-width:0;display:flex;flex-direction:column}
.hd{height:var(--hd);background:#fff;border-bottom:1px solid var(--line);display:flex;
align-items:center;gap:14px;padding:0 26px;position:sticky;top:0;z-index:20}
.hamb{display:none;border:none;background:none;font-size:18px;color:var(--c1);cursor:pointer}
.hd .ttl{font-size:16px;font-weight:800;color:var(--c0);letter-spacing:-.3px}
.hd .acct{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--c1);
background:var(--bg);border:1px solid var(--line);padding:7px 13px;border-radius:9px}
.hd .acct b{color:var(--c0);font-weight:700}
.hd .sp{flex:1}
.hd .conn{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600;color:var(--c1)}
.hd .conn .d{width:8px;height:8px;border-radius:50%;background:var(--green);
box-shadow:0 0 0 3px var(--green-soft)}.hd .conn.off .d{background:var(--red);box-shadow:0 0 0 3px var(--red-soft)}
.hd .upd{font-size:11px;color:var(--c2);text-align:right;line-height:1.45;font-variant-numeric:tabular-nums}
.btn{font-family:inherit;padding:8px 14px;border-radius:9px;font-size:12.5px;font-weight:600;
cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--c1);
display:inline-flex;align-items:center;gap:7px;transition:.15s}
.btn:hover{background:var(--bg);color:var(--c0)}
.body{padding:22px 26px;display:grid;grid-template-columns:minmax(0,1fr) 304px;gap:20px}
@media(max-width:1320px){.body{grid-template-columns:1fr}}
.col{display:flex;flex-direction:column;gap:20px;min-width:0}
.rail{display:flex;flex-direction:column;gap:20px}
/* kpi */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:var(--rd);padding:17px 18px;
box-shadow:var(--sh);transition:.18s}
.kpi:hover{box-shadow:var(--sh2);transform:translateY(-1px)}
.kpi .h{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--c1);font-weight:500}
.kpi .h .ic{width:26px;height:26px;border-radius:8px;display:flex;align-items:center;
justify-content:center;font-size:12px;background:var(--blue-soft);color:var(--blue)}
.kpi .h .ic.g{background:var(--green-soft);color:var(--green)}
.kpi .h .ic.r{background:var(--red-soft);color:var(--red)}
.kpi .h .ic.n{background:var(--bg);color:var(--c2)}
.kpi .v{font-size:23px;font-weight:800;color:var(--c0);margin-top:12px;letter-spacing:-.5px;
font-variant-numeric:tabular-nums;line-height:1.15}
.kpi .v.up{color:var(--green)}.kpi .v.down{color:var(--red)}
.kpi .v small{font-size:13px;font-weight:600;color:var(--c2);margin-left:3px}
.kpi .s{font-size:11.5px;margin-top:7px;color:var(--c2);font-variant-numeric:tabular-nums}
.kpi .s .u{color:var(--green);font-weight:700}.kpi .s .o{color:var(--red);font-weight:700}
/* card */
.card{background:#fff;border:1px solid var(--line);border-radius:var(--rd);box-shadow:var(--sh)}
.ch{display:flex;align-items:center;gap:10px;padding:16px 20px;border-bottom:1px solid var(--line)}
.ch .ct{font-size:14px;font-weight:700;color:var(--c0);flex:1;letter-spacing:-.2px}
.ch .ct i{color:var(--c3);margin-right:8px}
.seg{display:flex;gap:2px;background:var(--bg);padding:3px;border-radius:9px}
.sgb{border:none;background:none;color:var(--c2);font-family:inherit;font-weight:600;
font-size:11px;padding:5px 10px;border-radius:7px;cursor:pointer;transition:.12s}
.sgb:hover{color:var(--c0)}.sgb.on{background:#fff;color:var(--c0);box-shadow:var(--sh)}
.badge{padding:4px 11px;border-radius:20px;font-size:11px;font-weight:700;
display:inline-flex;align-items:center;gap:5px}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.badge.run{background:var(--green-soft);color:#0a7d57}
.badge.stop{background:var(--red-soft);color:#c23030}
.badge.part{background:var(--amber-soft);color:#b45309}
.chart-row{display:grid;grid-template-columns:1.7fr 1fr;gap:20px}
@media(max-width:1024px){.chart-row{grid-template-columns:1fr}}
.cw{position:relative;height:288px;padding:16px 18px}
.donut-wrap{position:relative;height:210px;padding:8px 0}
.donut-c{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
justify-content:center;pointer-events:none}
.donut-c span{font-size:11px;color:var(--c2)}
.donut-c b{font-size:19px;font-weight:800;color:var(--c0);margin-top:3px;letter-spacing:-.3px}
.lg{padding:4px 20px 18px;display:flex;flex-direction:column;gap:9px}
.lg .r{display:flex;align-items:center;gap:9px;font-size:12.5px}
.lg .r i{width:9px;height:9px;border-radius:3px;flex-shrink:0}
.lg .r .nm{flex:1;color:var(--c1)}.lg .r .pc{font-weight:700;color:var(--c0);font-variant-numeric:tabular-nums}
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th{text-align:left;color:var(--c2);font-weight:600;padding:11px 20px;
border-bottom:1px solid var(--line);font-size:10.5px;letter-spacing:.04em;
text-transform:uppercase;background:#fbfcfe}
.tbl td{padding:13px 20px;border-bottom:1px solid var(--line);color:var(--c1);
font-variant-numeric:tabular-nums}
.tbl tbody tr:hover{background:#fafbfe}.tbl tr:last-child td{border-bottom:0}
.tbl td b{color:var(--c0);font-weight:700}
.dotn{display:inline-flex;align-items:center;gap:8px}
.dotn i{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.r-up{color:var(--green);font-weight:700}.r-dn{color:var(--red);font-weight:700}
.tag{padding:3px 9px;border-radius:7px;font-size:11px;font-weight:700}
.tag.buy{background:var(--red-soft);color:#c23030}
.tag.sell{background:var(--blue-soft);color:var(--blue)}
.foot{padding:11px 20px;font-size:11px;color:var(--c2);border-top:1px solid var(--line);
display:flex;align-items:center;justify-content:space-between}
/* rail */
.rc{background:#fff;border:1px solid var(--line);border-radius:var(--rd);box-shadow:var(--sh)}
.rc .rh{display:flex;align-items:center;padding:15px 18px;border-bottom:1px solid var(--line);
font-size:13.5px;font-weight:700;color:var(--c0)}
.rc .rh .lk{margin-left:auto;font-size:11px;color:var(--blue);font-weight:600;cursor:pointer}
.sumrow{display:flex;align-items:center;justify-content:space-between;padding:12px 18px;
font-size:12.5px;border-bottom:1px solid var(--line)}
.sumrow:last-child{border-bottom:0}.sumrow .k{color:var(--c1)}
.sumrow .v{font-weight:700;color:var(--c0);font-variant-numeric:tabular-nums}
.bar{height:7px;border-radius:7px;background:var(--bg);overflow:hidden;display:flex;margin-top:3px}
.bar i{height:100%}.bar .w{background:var(--green)}.bar .l{background:var(--red)}
.stlist{padding:7px 18px 14px}
.strow{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line);font-size:12.5px}
.strow:last-child{border-bottom:0}
.strow .dd{width:8px;height:8px;border-radius:50%}.strow .dd.on{background:var(--green)}.strow .dd.off{background:var(--red)}
.strow .nm{flex:1;color:var(--c0);font-weight:600}
.alist{padding:6px 18px 14px}
.al{display:flex;gap:10px;padding:11px 0;border-bottom:1px solid var(--line);font-size:12px}
.al:last-child{border-bottom:0}
.al .ad{width:7px;height:7px;border-radius:50%;margin-top:5px;flex-shrink:0}
.al .ad.e{background:var(--red)}.al .ad.w{background:var(--amber)}.al .ad.i{background:var(--blue)}
.al .am{flex:1;color:var(--c1);line-height:1.55;word-break:break-all}
.al .at{display:block;color:var(--c2);font-size:10.5px;margin-top:3px;font-variant-numeric:tabular-nums}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;
height:100%;color:var(--c2);text-align:center;gap:9px;padding:24px}
.empty i{font-size:28px;color:var(--c3)}.empty .t{font-size:13px;font-weight:600;color:var(--c1)}
.empty .s{font-size:11px;color:var(--c2)}
.muted{color:var(--c2);font-size:12.5px;padding:26px 20px;text-align:center}
.hidden{display:none!important}
.scrim{display:none;position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:25}
@media(max-width:980px){.sb{transform:translateX(-100%);transition:.25s}.sb.open{transform:none}
.mn{margin-left:0}.hamb{display:block}.scrim.show{display:block}.body{padding:16px}}
.frame-card{display:flex;flex-direction:column;min-height:620px}
iframe{flex:1;width:100%;border:0;min-height:620px}
</style></head><body>
<div class="wrap">
  <aside class="sb" id="sb">
    <div class="brand"><div class="m"><i class="fa-solid fa-bolt"></i></div>
      <div><b>TRADING SUITE</b><small>AUTOTRADE</small></div></div>
    <nav class="nav" id="nav"></nav>
    <div class="sbcard"><b>단일 계좌 멀티전략</b><p id="sbc">공용 계좌 69567573<br>로딩 중…</p></div>
    <div class="sbfoot" id="foot">계좌 정보 불러오는 중…</div>
  </aside>
  <div class="scrim" id="scrim"></div>
  <main class="mn">
    <header class="hd">
      <button class="hamb" id="hamb"><i class="fa-solid fa-bars"></i></button>
      <div class="ttl" id="ttl">자동매매 대시보드</div>
      <div class="acct"><i class="fa-solid fa-building-columns" style="color:#94a3b8"></i>
        <span>실계좌</span><b>69567573</b></div>
      <div class="sp"></div>
      <div class="conn" id="conn"><span class="d"></span><span id="connt">연결 정상</span></div>
      <div class="upd" id="upd"></div>
      <button class="btn" id="refresh"><i class="fa-solid fa-rotate"></i> 새로고침</button>
    </header>
    <div class="body">
      <div class="col">
        <section id="overview">
          <div class="kpis" id="kpi"></div>
          <div class="chart-row" style="margin-top:20px">
            <div class="card">
              <div class="ch"><span class="ct"><i class="fa-solid fa-chart-line"></i>전략별 누적수익률</span>
                <div class="seg" id="seg1"></div></div>
              <div class="cw" id="cw1"><canvas id="ch1"></canvas></div>
            </div>
            <div class="card">
              <div class="ch"><span class="ct"><i class="fa-solid fa-chart-pie"></i>전략 배분 현황</span></div>
              <div class="donut-wrap"><canvas id="ch2"></canvas>
                <div class="donut-c"><span>총 투입금액</span><b id="dtot">—</b></div></div>
              <div class="lg" id="lg2"></div>
            </div>
          </div>
          <div class="card" style="margin-top:20px">
            <div class="ch"><span class="ct"><i class="fa-solid fa-table-list"></i>전략별 상세 성과</span></div>
            <div id="pstrat" style="overflow-x:auto"><div class="muted">불러오는 중…</div></div>
            <div class="foot"><span>※ 원금은 활성 종목 시드 합계, 수익률·누적손익은 완료 싸이클 실현 기준</span></div>
          </div>
          <div class="card" style="margin-top:20px">
            <div class="ch"><span class="ct"><i class="fa-solid fa-receipt"></i>최근 매매 내역</span></div>
            <div id="ptrade" style="overflow-x:auto"><div class="muted">불러오는 중…</div></div>
          </div>
        </section>
        <section id="stratview" class="hidden">
          <div class="card frame-card">
            <div class="ch"><span class="ct" id="fctitle">전략 대시보드</span>
              <span class="badge" id="fcbadge"></span></div>
            <iframe id="frame" title="strategy dashboard"></iframe>
          </div>
        </section>
      </div>
      <aside class="rail" id="rail">
        <div class="rc"><div class="rh">계좌 현황 요약</div><div id="rsum"></div></div>
        <div class="rc"><div class="rh">전략 가동 상태</div><div class="stlist" id="rstr"></div></div>
        <div class="rc"><div class="rh">시스템 알림 · 오류</div><div class="alist" id="ralert"></div></div>
      </aside>
    </div>
  </main>
</div>
<script>
var STRATS=__STRATS__;var view='ov';var SER=null;var R1='6M';
var CH1=null;var CH2=null;var PAL=['#3b6ef5','#10b981','#6c5ce7','#f59e0b','#ef4444'];
var nav=document.getElementById('nav');
function fmt(n){return (n==null||n==='')?'—':'$'+Number(n).toLocaleString(undefined,{maximumFractionDigits:0});}
function fmt2(n){return (n==null||n==='')?'—':'$'+Number(n).toLocaleString(undefined,{maximumFractionDigits:2});}
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,function(m){
 return {'&':'&amp;','<':'&lt;','>':'&gt;'}[m];});}
function dirCls(n){return n==null?'':(n>=0?'up':'down');}
function signMoney(n){if(n==null)return '<span style="color:#94a3b8">—</span>';
 return '<span class="'+(n>=0?'r-up':'r-dn')+'">'+(n>=0?'+':'-')+
 '$'+Number(Math.abs(n)).toLocaleString(undefined,{maximumFractionDigits:0})+'</span>';}
function signPct(n){if(n==null)return '<span style="color:#94a3b8">—</span>';
 return '<span class="'+(n>=0?'r-up':'r-dn')+'">'+(n>=0?'+':'')+Number(n).toFixed(2)+'%</span>';}
function buildNav(){
 var h='<div class="ng">현황</div>'+
  '<button class="ni on" data-i="0"><span class="i"><i class="fa-solid fa-gauge-high"></i></span>'+
  '<span class="t"><b>통합 대시보드</b><span>계좌·전략·차트</span></span></button>'+
  '<div class="ng">전략</div>';
 STRATS.forEach(function(s,i){h+='<button class="ni" data-i="'+(i+1)+'">'+
  '<span class="i"><i class="fa-solid '+s.icon+'"></i></span><span class="t"><b>'+
  esc(s.label)+'</b><span>'+esc(s.sub)+'</span></span><span class="d" id="nd'+i+'"></span></button>';});
 nav.innerHTML=h;
 [].forEach.call(nav.querySelectorAll('.ni'),function(b){b.onclick=function(){pick(+b.dataset.i);};});}
function pick(i){[].forEach.call(nav.querySelectorAll('.ni'),function(b){
  b.classList.toggle('on',+b.dataset.i===i);});closeSb();
 if(i===0){view='ov';document.getElementById('overview').classList.remove('hidden');
  document.getElementById('stratview').classList.add('hidden');
  document.getElementById('ttl').textContent='자동매매 대시보드';loadMetrics();loadSeries();
 }else{view='st';var s=STRATS[i-1];
  document.getElementById('overview').classList.add('hidden');
  document.getElementById('stratview').classList.remove('hidden');
  var f=document.getElementById('frame');if(f.src.indexOf(s.path)<0)f.src=s.path;
  document.getElementById('ttl').textContent=s.label;
  document.getElementById('fctitle').textContent=s.label+' 대시보드';}}
function kc(lab,ic,icc,val,unit,vcls,sub){return '<div class="kpi"><div class="h">'+
 '<span class="ic '+(icc||'')+'"><i class="fa-solid '+ic+'"></i></span>'+lab+'</div>'+
 '<div class="v '+(vcls||'')+'">'+val+(unit?'<small>'+unit+'</small>':'')+'</div>'+
 '<div class="s">'+(sub||'')+'</div></div>';}
function renderKPI(a){var rp=a.total_return_pct;
 document.getElementById('kpi').innerHTML=
  kc('총 평가자산','fa-wallet','b',fmt(a.total_assets),'',' ',
   '순투입 '+fmt(a.net_invested))+
  kc('누적손익','fa-coins',(a.total_pnl>=0?'g':'r'),
   (a.total_pnl>=0?'+':'')+fmt(a.total_pnl),'',dirCls(a.total_pnl),
   '미실현 평가손익')+
  kc('총 수익률','fa-chart-pie',(rp>=0?'g':'r'),
   (rp==null?'—':(rp>=0?'+':'')+Number(rp).toFixed(2)+'%'),'',dirCls(rp),
   'MDD '+(a.mdd_pct==null?'수집중':Number(a.mdd_pct).toFixed(2)+'%'))+
  kc('실현손익','fa-circle-check',(a.realized_pnl>=0?'g':'r'),
   (a.realized_pnl>=0?'+':'')+fmt(a.realized_pnl),'',dirCls(a.realized_pnl),
   '완료 싸이클 누적')+
  kc('현금','fa-money-bill-wave','n',fmt(a.cash),'',' ',
   '현금비중 '+(a.cash_ratio==null?'—':Number(a.cash_ratio).toFixed(1)+'%'))+
  kc('주식 평가','fa-layer-group','n',fmt(a.total_assets-a.cash>0?a.total_assets-a.cash:0),'',' ',
   '평가자산 중 주식분');}
function renderStrat(arr){var w=document.getElementById('pstrat');
 if(!arr||!arr.length){w.innerHTML='<div class="muted">데이터 없음</div>';return;}
 w.innerHTML='<table class="tbl"><thead><tr><th>전략명</th><th style="text-align:right">원금</th>'+
  '<th style="text-align:right">누적손익</th><th style="text-align:right">수익률</th>'+
  '<th style="text-align:right">승률</th><th style="text-align:right">보유</th>'+
  '<th style="text-align:right">MDD</th><th>상태</th></tr></thead><tbody>'+
  arr.map(function(s,i){
   var st=s.kill_switch?'<span class="badge stop">정지</span>':'<span class="badge run">운영중</span>';
   var md=s.mdd_pct==null?'<span style="color:#94a3b8">수집중</span>':signPct(s.mdd_pct);
   return '<tr><td><span class="dotn"><i style="background:'+PAL[i%PAL.length]+'"></i><b>'+
   esc(s.display_name)+'</b></span></td>'+
   '<td style="text-align:right">'+fmt(s.invested)+'</td>'+
   '<td style="text-align:right">'+signMoney(s.realized_pnl)+'</td>'+
   '<td style="text-align:right">'+signPct(s.return_pct)+'</td>'+
   '<td style="text-align:right">'+(s.win_rate==null?'—':Number(s.win_rate).toFixed(1)+'%')+'</td>'+
   '<td style="text-align:right">'+s.holdings_count+'종목</td>'+
   '<td style="text-align:right">'+md+'</td><td>'+st+'</td></tr>';}).join('')+'</tbody></table>';}
function renderTrades(ts){var w=document.getElementById('ptrade');
 if(!ts||!ts.length){w.innerHTML='<div class="muted">매매 내역 없음</div>';return;}
 w.innerHTML='<table class="tbl"><thead><tr><th>체결일</th><th>전략명</th><th>종목</th>'+
  '<th>매매구분</th><th style="text-align:right">수량</th><th style="text-align:right">체결가</th>'+
  '<th style="text-align:right">주문금액</th></tr></thead><tbody>'+
  ts.map(function(t){var sd=t.side==='buy'?'<span class="tag buy">매수</span>':'<span class="tag sell">매도</span>';
  return '<tr><td>'+esc(t.trade_date)+'</td><td>'+esc(t.display_name)+'</td><td><b>'+
  esc(t.ticker)+'</b></td><td>'+sd+'</td><td style="text-align:right">'+t.qty+
  '</td><td style="text-align:right">'+fmt2(t.price)+'</td><td style="text-align:right">'+
  fmt2(t.amount)+'</td></tr>';}).join('')+'</tbody></table>';}
function renderRail(d){var a=d.account||{};var ss=d.strategies||[];var au=d.automation||{};
 var stockEvlu=(a.total_assets-a.cash>0)?(a.total_assets-a.cash):0;
 var hc=0;ss.forEach(function(s){hc+=s.holdings_count||0;});
 document.getElementById('rsum').innerHTML=
  '<div class="sumrow"><span class="k">보유 종목 수</span><span class="v">'+hc+' 개</span></div>'+
  '<div class="sumrow"><span class="k">주식 평가금액</span><span class="v">'+fmt(stockEvlu)+'</span></div>'+
  '<div class="sumrow"><span class="k">총 수익률</span><span class="v">'+signPct(a.total_return_pct)+'</span></div>'+
  '<div class="sumrow"><span class="k">실현손익</span><span class="v">'+signMoney(a.realized_pnl)+'</span></div>'+
  '<div class="sumrow"><span class="k">가동 전략</span><span class="v">'+(au.active||0)+' / '+(au.total||0)+'</span></div>';
 document.getElementById('rstr').innerHTML=ss.map(function(s,i){
  return '<div class="strow"><span class="dd '+(s.kill_switch?'off':'on')+'"></span>'+
  '<span class="nm">'+esc(s.display_name)+'</span>'+signPct(s.return_pct)+'</div>';}).join('');
 var rows=[];ss.forEach(function(s){(s.errors||[]).forEach(function(l){
  rows.push({lv:l.level,m:'['+s.display_name+'] '+l.message,t:l.created_at});});});
 rows.sort(function(a,b){return (b.t||'').localeCompare(a.t||'');});
 var el=document.getElementById('ralert');
 if(!rows.length){el.innerHTML='<div class="al"><span class="ad i"></span>'+
  '<span class="am">자동매매 시스템이 정상적으로 운영 중입니다.<span class="at">최근 오류 없음</span></span></div>';}
 else{el.innerHTML=rows.slice(0,8).map(function(r){
  var c=r.lv==='ERROR'?'e':(r.lv==='WARNING'?'w':'i');
  return '<div class="al"><span class="ad '+c+'"></span><span class="am">'+esc(r.m)+
  '<span class="at">'+esc((r.t||'').replace('T',' ').slice(0,19))+'</span></span></div>';}).join('');}}
function loadMetrics(){fetch('/api/suite/metrics').then(function(r){return r.json();})
 .then(function(d){renderKPI(d.account||{});renderStrat(d.strategies||[]);
  renderTrades(d.recent_trades||[]);renderRail(d);drawDonut(d.strategies||[]);
  (d.strategies||[]).forEach(function(s,i){var x=document.getElementById('nd'+i);
   if(x)x.className='d '+(s.kill_switch?'off':'on');});
  var au=d.automation||{};var cn=document.getElementById('conn');
  cn.className='conn'+(au.running?'':' off');
  document.getElementById('connt').textContent=au.running?'연결 정상':'정지 상태';
  document.getElementById('upd').innerHTML='마지막 업데이트<br>'+
   esc((d.generated_at||'').replace('T',' ').slice(0,19));
  document.getElementById('sbc').innerHTML='공용 계좌 69567573 · real<br>가동 '+
   (au.active||0)+'/'+(au.total||0)+' 전략';
  document.getElementById('foot').innerHTML='<b>스냅샷</b> '+
   esc(((d.account&&d.account.snapshot_at)||'').replace('T',' ').slice(0,16))+
   '<br>고객 운영 · trading_suite';})
 .catch(function(){document.getElementById('pstrat').innerHTML='<div class="muted">지표 로드 실패</div>';});}
function drawDonut(ss){if(CH2){CH2.destroy();CH2=null;}
 var lab=[],val=[],tot=0;
 ss.forEach(function(s){var v=s.invested||0;if(v>0){lab.push(s.display_name);val.push(v);tot+=v;}});
 document.getElementById('dtot').textContent=tot?fmt(tot):'—';
 var lg=document.getElementById('lg2');
 if(!tot){lg.innerHTML='<div class="muted" style="padding:8px 0">투입금액 데이터 없음</div>';
  document.getElementById('ch2').style.display='none';return;}
 document.getElementById('ch2').style.display='';
 CH2=new Chart(document.getElementById('ch2'),{type:'doughnut',
  data:{labels:lab,datasets:[{data:val,backgroundColor:PAL,borderWidth:2,borderColor:'#fff'}]},
  options:{responsive:true,maintainAspectRatio:false,cutout:'68%',
   plugins:{legend:{display:false},tooltip:{backgroundColor:'#0f172a',padding:10,cornerRadius:8,
    callbacks:{label:function(c){return ' '+c.label+': '+fmt(c.parsed)+' ('+
     (c.parsed/tot*100).toFixed(1)+'%)';}}}}}});
 lg.innerHTML=lab.map(function(n,i){return '<div class="r"><i style="background:'+
  PAL[i%PAL.length]+'"></i><span class="nm">'+esc(n)+'</span><span class="pc">'+
  (val[i]/tot*100).toFixed(1)+'%</span></div>';}).join('');}
function days(r){return {'1주':7,'1개월':30,'3개월':90,'6개월':180,'1년':365,'전체':99999}[r]||180;}
function lbl(ts){return String(ts).slice(5,10).replace('-','/');}
function lineOpts(){return {responsive:true,maintainAspectRatio:false,layout:{padding:{top:6}},
 interaction:{mode:'index',intersect:false},
 plugins:{legend:{position:'bottom',labels:{usePointStyle:true,pointStyle:'circle',
  boxWidth:7,boxHeight:7,padding:15,font:{size:11.5,family:"'Noto Sans KR'"},color:'#475569'}},
  tooltip:{backgroundColor:'#0f172a',titleColor:'#fff',bodyColor:'#e2e8f0',padding:11,
   cornerRadius:9,boxPadding:5,callbacks:{label:function(c){
   return ' '+c.dataset.label+': '+Number(c.parsed.y).toFixed(2)+'%';}}}},
 scales:{x:{grid:{display:false},border:{display:false},
  ticks:{color:'#94a3b8',font:{size:10.5},maxTicksLimit:7,maxRotation:0,padding:6}},
 y:{grid:{color:'#eef1f6'},border:{display:false},
  ticks:{color:'#94a3b8',font:{size:10.5},padding:8,maxTicksLimit:6,
  callback:function(v){return v+'%';}}}}};}
function setEmpty(t,s){var w=document.getElementById('cw1');
 w.innerHTML='<div class="empty"><i class="fa-solid fa-chart-line"></i>'+
 '<div class="t">'+t+'</div><div class="s">'+s+'</div></div>';}
function drawLine(){if(!SER)return;
 if(SER.collecting||!SER.points||SER.points.length<2){
  setEmpty('수익률 데이터 수집중','equity 스냅샷이 30분 주기로 누적되면 자동 표시됩니다');return;}
 if(CH1){CH1.destroy();CH1=null;}
 document.getElementById('cw1').innerHTML='<canvas id="ch1"></canvas>';
 var n=days(R1);var pts=SER.points;var last=new Date(pts[pts.length-1].ts);
 var cut=new Date(last.getTime()-n*864e5);
 var idx=pts.map(function(p,i){return {p:p,i:i};}).filter(function(x){
  return new Date(x.p.ts)>=cut;});
 var off=idx.length?idx[0].i:0;var L=idx.map(function(x){return lbl(x.p.ts);});
 var ds=[];var j=0;for(var k in (SER.strategy_return||{})){
  var nm=(STRATS.filter(function(s){return s.key===k;})[0]||{}).label||k;
  ds.push({label:nm,data:SER.strategy_return[k].slice(off),borderColor:PAL[j%PAL.length],
   backgroundColor:'transparent',borderWidth:2.4,pointRadius:0,tension:.35});j++;}
 CH1=new Chart(document.getElementById('ch1'),{type:'line',
  data:{labels:L,datasets:ds},options:lineOpts()});}
function loadSeries(){fetch('/api/suite/series').then(function(r){return r.json();})
 .then(function(d){SER=d;drawLine();}).catch(function(){
  setEmpty('수익률 로드 실패','잠시 후 새로고침해 주세요');});}
function buildSeg(){var box=document.getElementById('seg1');
 ['1주','1개월','3개월','6개월','1년','전체'].forEach(function(r){var b=document.createElement('button');
  b.className='sgb'+(r==='6개월'?' on':'');b.textContent=r;b.onclick=function(){
   [].forEach.call(box.children,function(x){x.classList.remove('on');});b.classList.add('on');
   R1=r;drawLine();};box.appendChild(b);});}
function closeSb(){document.getElementById('sb').classList.remove('open');
 document.getElementById('scrim').classList.remove('show');}
document.getElementById('hamb').onclick=function(){
 document.getElementById('sb').classList.toggle('open');
 document.getElementById('scrim').classList.toggle('show');};
document.getElementById('scrim').onclick=closeSb;
document.getElementById('refresh').onclick=function(){
 if(view==='ov'){loadMetrics();loadSeries();}
 else{var f=document.getElementById('frame');if(f.src)f.src=f.src;}};
buildSeg();buildNav();pick(0);
setInterval(function(){if(view==='ov'){loadMetrics();loadSeries();}},60000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    """통합 포털 셸 (KAIROS 스타일). 좌측 전략 네비 + 상단 계좌바 + KPI/차트/표/레일."""
    from core.strategy_adapters import DISPLAY_NAMES
    import json as _json
    strategies = [
        {
            "key": k,
            "label": DISPLAY_NAMES.get(k, k),
            "path": f"/{k}/dashboard",
            "sub": _STRAT_META.get(k, {}).get("sub", k),
            "icon": _STRAT_META.get(k, {}).get("icon", "fa-chart-line"),
        }
        for k in SUB_APPS
    ]
    html = _SHELL_HTML.replace("__STRATS__", _json.dumps(strategies, ensure_ascii=False))
    return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
