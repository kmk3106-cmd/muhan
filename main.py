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

# 마운트 경로 → sub-app (P5 통합 셸/4전략 확장 시 데이터 주도로 전환)
SUB_APPS = {"infinite": infinite_app, "ddsop": ddsop_app}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        for name, sub in SUB_APPS.items():
            await stack.enter_async_context(sub.router.lifespan_context(sub))
            logger.info(f"[suite] sub-app lifespan 기동: {name}")
        # equity 스냅샷터: DB만 읽음(KIS 미호출) → 레인 무관. 30분 주기 + 기동 1분 후 1회
        sched = None
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from core.equity_snapshot import snapshot as _equity_snapshot
            sched = BackgroundScheduler(timezone="Asia/Seoul")
            sched.add_job(_equity_snapshot, "interval", minutes=30,
                          id="equity_snapshot", max_instances=1)
            # 기동 직후 빠르게 2포인트 적재 → 차트가 곧바로 표시됨
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
    """전략별 활성 티커 + 시드 예산 가드레일 (통합 대시보드용)."""
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
    """차트용 시계열 (자산추이 · 전략별 누적수익률). equity 스냅샷 누적분."""
    from core.equity_snapshot import series
    return series()


@app.post("/api/suite/strategies/{name}/budget")
def set_strategy_budget(name: str, body: BudgetBody):
    """전략별 시드 할당 총액 설정 (사용자 명시 할당)."""
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
<title>trading_suite · 통합 운영 포털</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/css/all.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--primary:#4f86f7;--primary-dark:#3a6fd8;--primary-soft:#eaf1fe;--purple:#6c5ce7;
--success:#10b981;--success-soft:#e7f7f0;--warning:#f59e0b;--danger:#ef4444;--danger-soft:#fdeced;
--gray-50:#f7f8fa;--gray-100:#f1f3f6;--gray-200:#e8eaf0;--gray-300:#d4d8e0;--gray-400:#9aa1ad;
--gray-500:#6b7280;--gray-600:#4b5563;--gray-700:#374151;--gray-800:#1f2937;--gray-900:#111827;
--sidebar-width:248px;--header-height:62px;
--sh:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
--sh-lg:0 4px 16px rgba(16,24,40,.07);--radius:14px}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:'Noto Sans KR',sans-serif;background:var(--gray-50);color:var(--gray-800);
-webkit-font-smoothing:antialiased;font-size:13px}
.app-layout{display:flex;min-height:100vh}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:var(--gray-300);
border-radius:9px;border:2px solid transparent;background-clip:content-box}
.sidebar{width:var(--sidebar-width);background:#fff;border-right:1px solid var(--gray-200);
display:flex;flex-direction:column;position:fixed;top:0;bottom:0;left:0;z-index:30}
.sidebar-brand{padding:0 20px;height:var(--header-height);display:flex;align-items:center;
gap:11px;flex-shrink:0;border-bottom:1px solid var(--gray-200)}
.sidebar-brand .logo{width:34px;height:34px;border-radius:10px;color:#fff;
background:linear-gradient(135deg,var(--primary),var(--purple));
display:flex;align-items:center;justify-content:center;font-size:15px;
box-shadow:0 4px 10px rgba(79,134,247,.28)}
.sidebar-brand b{font-size:15px;font-weight:700;letter-spacing:-.2px;color:var(--gray-900)}
.sidebar-brand small{display:block;font-size:11px;color:var(--gray-400);font-weight:400;margin-top:1px}
.sidebar-nav{flex:1;padding:8px 12px;overflow-y:auto}
.nav-group{padding:16px 12px 7px;font-size:10.5px;font-weight:700;color:var(--gray-400);
letter-spacing:.08em;text-transform:uppercase}
.nav-item{display:flex;align-items:center;gap:11px;width:100%;text-align:left;border:none;
background:none;cursor:pointer;padding:10px 12px;border-radius:10px;color:var(--gray-600);
font-family:inherit;font-size:13px;margin-bottom:2px;transition:.15s;position:relative}
.nav-item:hover{background:var(--gray-100);color:var(--gray-900)}
.nav-item.active{background:var(--gray-900);color:#fff;box-shadow:var(--sh)}
.nav-item .ic{width:18px;text-align:center;font-size:14px;flex-shrink:0}
.nav-item.active .ic{color:#fff}.nav-item .ic{color:var(--gray-400)}
.nav-item:hover .ic{color:var(--gray-600)}
.nav-item .tx{flex:1;min-width:0}
.nav-item .tx b{font-weight:600;display:block;font-size:13px}
.nav-item .tx span{font-size:11px;color:var(--gray-400);font-weight:400}
.nav-item.active .tx span{color:rgba(255,255,255,.6)}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--gray-300);
box-shadow:0 0 0 3px var(--gray-100)}
.nav-item.active .dot{box-shadow:0 0 0 3px rgba(255,255,255,.12)}
.dot.on{background:var(--success)}.dot.off{background:var(--danger)}
.sidebar-foot{border-top:1px solid var(--gray-200);padding:14px 20px;font-size:11px;
color:var(--gray-500);line-height:1.75}
.sidebar-foot b{color:var(--gray-700);font-weight:600}
.main{flex:1;margin-left:var(--sidebar-width);display:flex;flex-direction:column;min-width:0}
.topbar{min-height:var(--header-height);background:rgba(255,255,255,.85);backdrop-filter:blur(8px);
border-bottom:1px solid var(--gray-200);display:flex;align-items:center;gap:16px;padding:12px 28px;
position:sticky;top:0;z-index:20}
.hamb{display:none;border:none;background:none;font-size:18px;color:var(--gray-700);cursor:pointer}
.page-title{font-size:18px;font-weight:700;color:var(--gray-900);flex:1;letter-spacing:-.3px}
.page-title small{font-size:12.5px;font-weight:400;color:var(--gray-400);margin-left:10px}
.btn{font-family:inherit;padding:9px 15px;border-radius:9px;font-size:12.5px;font-weight:600;
cursor:pointer;border:none;display:inline-flex;align-items:center;gap:7px;transition:.15s}
.btn-secondary{background:#fff;color:var(--gray-600);border:1px solid var(--gray-200);box-shadow:var(--sh)}
.btn-secondary:hover{background:var(--gray-100);color:var(--gray-900)}
.content{padding:24px 28px;display:flex;flex-direction:column;gap:20px}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(218px,1fr));gap:16px}
.kpi{background:#fff;border:1px solid var(--gray-200);border-radius:var(--radius);
padding:20px;box-shadow:var(--sh);transition:.18s}
.kpi:hover{box-shadow:var(--sh-lg);transform:translateY(-1px)}
.kpi .top{display:flex;align-items:center;justify-content:space-between}
.kpi .lab{font-size:12px;color:var(--gray-500);font-weight:500}
.kpi .ico{width:34px;height:34px;border-radius:10px;display:flex;align-items:center;
justify-content:center;font-size:14px;background:var(--gray-100);color:var(--gray-500)}
.kpi .ico.b{background:var(--primary-soft);color:var(--primary)}
.kpi .ico.g{background:var(--success-soft);color:var(--success)}
.kpi .ico.r{background:var(--danger-soft);color:var(--danger)}
.kpi .val{font-size:27px;font-weight:800;color:var(--gray-900);margin-top:14px;
letter-spacing:-.6px;font-variant-numeric:tabular-nums;line-height:1.1}
.kpi .val.up{color:var(--success)}.kpi .val.down{color:var(--danger)}
.kpi .sub{font-size:12px;margin-top:7px;color:var(--gray-400);font-variant-numeric:tabular-nums}
.chip{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:7px;
font-size:11.5px;font-weight:700;font-variant-numeric:tabular-nums}
.chip.up{background:var(--success-soft);color:#0a7d57}.chip.down{background:var(--danger-soft);color:#c23030}
.kpi.auto{display:flex;align-items:center;gap:16px}
.kpi.auto .pl{width:52px;height:52px;border-radius:14px;background:var(--success-soft);
color:var(--success);display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0}
.kpi.auto.off .pl{background:var(--danger-soft);color:var(--danger)}
.kpi.auto .av{font-size:22px;font-weight:800;color:var(--gray-900);letter-spacing:-.4px;margin-top:2px}
.card{background:#fff;border:1px solid var(--gray-200);border-radius:var(--radius);
overflow:hidden;box-shadow:var(--sh)}
.card-header{padding:17px 22px;border-bottom:1px solid var(--gray-200);display:flex;
align-items:center;gap:10px}
.card-header .ct{font-size:14.5px;font-weight:700;color:var(--gray-900);flex:1;letter-spacing:-.2px}
.card-header .ct i{color:var(--gray-300);margin-right:8px}
.badge{padding:4px 11px;border-radius:20px;font-size:11px;font-weight:700;display:inline-flex;
align-items:center;gap:5px}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.85}
.badge.run{background:var(--success-soft);color:#0a7d57}
.badge.stop{background:var(--danger-soft);color:#c23030}
.frame-card{flex:1;display:flex;flex-direction:column;min-height:600px}
iframe{flex:1;width:100%;border:0;background:#fff;min-height:600px}
.ov{color:var(--danger);font-weight:600}
.scrim{display:none;position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:25}
@media(max-width:980px){
.sidebar{transform:translateX(-100%);transition:transform .25s;box-shadow:var(--sh-lg)}
.sidebar.open{transform:none}.main{margin-left:0}.hamb{display:block}
.scrim.show{display:block}}
.pos{color:var(--success);font-weight:600}.neg{color:var(--danger);font-weight:600}
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th{text-align:left;color:var(--gray-400);font-weight:600;padding:11px 22px;
border-bottom:1px solid var(--gray-200);font-size:10.5px;letter-spacing:.05em;
text-transform:uppercase;background:var(--gray-50)}
.tbl td{padding:12px 22px;border-bottom:1px solid var(--gray-100);color:var(--gray-700);
font-variant-numeric:tabular-nums}
.tbl tbody tr{transition:background .12s}.tbl tbody tr:hover{background:var(--gray-50)}
.tbl tr:last-child td{border-bottom:0}
.tbl td b{color:var(--gray-900);font-weight:700}
.tag{padding:3px 9px;border-radius:7px;font-size:11px;font-weight:700}
.tag.buy{background:var(--primary-soft);color:var(--primary-dark)}
.tag.sell{background:var(--danger-soft);color:#c23030}
.elist{padding:6px 22px 16px}
.eitem{display:flex;gap:11px;align-items:flex-start;padding:11px 0;
border-bottom:1px solid var(--gray-100);font-size:12.5px}
.eitem:last-child{border-bottom:0}
.lvl{padding:2px 8px;border-radius:6px;font-size:10px;font-weight:800;height:fit-content;
letter-spacing:.03em;flex-shrink:0}
.lvl.ERROR{background:var(--danger-soft);color:#c23030}
.lvl.WARNING{background:#fdf4e3;color:#b45309}
.emsg{flex:1;color:var(--gray-700);word-break:break-all;line-height:1.5}
.etime{color:var(--gray-400);white-space:nowrap;font-size:11px;font-variant-numeric:tabular-nums}
.muted{color:var(--gray-400);font-size:13px;padding:30px 22px;text-align:center}
.hidden{display:none!important}
.upd{font-size:11px;color:var(--gray-400);text-align:right;line-height:1.5;
font-variant-numeric:tabular-nums}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:1100px){.charts{grid-template-columns:1fr}}
.chwrap{position:relative;height:316px;padding:16px 18px 18px}
.seg{display:flex;gap:3px;background:var(--gray-100);padding:3px;border-radius:9px}
.segb{border:none;background:none;color:var(--gray-500);font-family:inherit;font-weight:600;
font-size:11.5px;padding:5px 11px;border-radius:7px;cursor:pointer;transition:.15s}
.segb:hover{color:var(--gray-800)}
.segb.on{background:#fff;color:var(--gray-900);box-shadow:var(--sh)}
.panels{display:grid;grid-template-columns:1.3fr 1fr 1.25fr;gap:20px}
@media(max-width:1200px){.panels{grid-template-columns:1fr}}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;
color:var(--gray-400);text-align:center;gap:10px;padding:20px}
.empty i{font-size:30px;color:var(--gray-300)}
.empty .t{font-size:13px;font-weight:600;color:var(--gray-500)}
.empty .s{font-size:11.5px;color:var(--gray-400)}
</style></head><body>
<div class="app-layout">
  <aside class="sidebar" id="sb">
    <div class="sidebar-brand">
      <div class="logo"><i class="fa-solid fa-layer-group"></i></div>
      <div><b>trading_suite</b><small>멀티전략 운영 포털</small></div>
    </div>
    <nav class="sidebar-nav" id="nav"></nav>
    <div class="sidebar-foot" id="foot">계좌 정보 불러오는 중…</div>
  </aside>
  <div class="scrim" id="scrim"></div>
  <main class="main">
    <header class="topbar">
      <button class="hamb" id="hamb"><i class="fa-solid fa-bars"></i></button>
      <div class="page-title" id="ptitle">통합 대시보드</div>
      <div class="upd" id="upd"></div>
      <button class="btn btn-secondary" id="refresh"><i class="fa-solid fa-rotate"></i> 새로고침</button>
    </header>
    <div class="content">
      <section id="overview">
        <div class="kpi-grid" id="kpi"></div>
        <div class="charts" style="margin-top:18px">
          <div class="card">
            <div class="card-header"><span class="ct"><i class="fa-solid fa-chart-area"></i>전체 자산추이</span>
              <div class="seg" id="seg1"></div></div>
            <div class="chwrap" id="cw1"><canvas id="ch1"></canvas></div>
          </div>
          <div class="card">
            <div class="card-header"><span class="ct"><i class="fa-solid fa-chart-line"></i>전략별 누적수익률</span>
              <div class="seg" id="seg2"></div></div>
            <div class="chwrap" id="cw2"><canvas id="ch2"></canvas></div>
          </div>
        </div>
        <div class="panels" style="margin-top:18px">
          <div class="card"><div class="card-header"><span class="ct">전략 성과 요약</span></div>
            <div id="pstrat" style="overflow-x:auto"><div class="muted">불러오는 중…</div></div></div>
          <div class="card"><div class="card-header"><span class="ct">보유종목 현황</span></div>
            <div id="phold" style="overflow-x:auto"><div class="muted">불러오는 중…</div></div></div>
          <div class="card"><div class="card-header"><span class="ct">최근 매매 로그</span></div>
            <div id="ptrade" style="overflow-x:auto"><div class="muted">불러오는 중…</div></div></div>
        </div>
        <div class="card" style="margin-top:18px">
          <div class="card-header"><span class="ct">주문 · API 오류상태</span></div>
          <div id="elog"><div class="muted">불러오는 중…</div></div>
        </div>
      </section>
      <section id="stratview" class="hidden">
        <div class="card frame-card">
          <div class="card-header"><span class="ct" id="fctitle">전략 대시보드</span>
            <span class="badge" id="fcbadge"></span></div>
          <iframe id="frame" title="strategy dashboard"></iframe>
        </div>
      </section>
    </div>
  </main>
</div>
<script>
var STRATS=__STRATS__;var view='ov';var curi=0;var SER=null;var R1='6M';var R2='6M';
var CH1=null;var CH2=null;var PAL=['#4f86f7','#ef4444','#10b981','#f59e0b','#6c5ce7'];
var nav=document.getElementById('nav');
function fmt(n){return (n==null||n==='')?'—':'$'+Number(n).toLocaleString(undefined,{maximumFractionDigits:2});}
function sgn(n){if(n==null)return '<span class="muted" style="padding:0">—</span>';
 var c=n>=0?'pos':'neg';return '<span class="'+c+'">'+(n>=0?'+':'')+
 '$'+Number(Math.abs(n)).toLocaleString(undefined,{maximumFractionDigits:2})+'</span>';}
function pctv(n){if(n==null)return '<span class="muted" style="padding:0">—</span>';
 var c=n>=0?'pos':'neg';return '<span class="'+c+'">'+(n>=0?'+':'')+Number(n).toFixed(2)+'%</span>';}
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,function(m){
 return {'&':'&amp;','<':'&lt;','>':'&gt;'}[m];});}
function buildNav(){
 var html='<div class="nav-group">현황</div>';
 html+='<button class="nav-item active" data-idx="0"><span class="ic">'+
  '<i class="fa-solid fa-gauge-high"></i></span><span class="tx"><b>통합 대시보드</b>'+
  '<span>계좌·전략·차트</span></span></button>';
 html+='<div class="nav-group">전략</div>';
 STRATS.forEach(function(s,i){html+='<button class="nav-item" data-idx="'+(i+1)+'">'+
  '<span class="ic"><i class="fa-solid '+s.icon+'"></i></span><span class="tx"><b>'+
  esc(s.label)+'</b><span>'+esc(s.sub)+'</span></span><span class="dot" id="dot'+i+'"></span></button>';});
 nav.innerHTML=html;
 [].forEach.call(nav.querySelectorAll('.nav-item'),function(b){
  b.onclick=function(){pick(+b.dataset.idx);};});}
function setActive(idx){[].forEach.call(nav.querySelectorAll('.nav-item'),function(b){
 b.classList.toggle('active',+b.dataset.idx===idx);});}
function pick(idx){setActive(idx);closeSb();
 if(idx===0){view='ov';curi=0;
  document.getElementById('overview').classList.remove('hidden');
  document.getElementById('stratview').classList.add('hidden');
  document.getElementById('ptitle').innerHTML='통합 대시보드 <small>공용계좌 69567573 · 멀티전략</small>';
  loadMetrics();loadSeries();
 }else{view='st';curi=idx-1;var s=STRATS[curi];
  document.getElementById('overview').classList.add('hidden');
  document.getElementById('stratview').classList.remove('hidden');
  var f=document.getElementById('frame');if(f.src.indexOf(s.path)<0)f.src=s.path;
  document.getElementById('ptitle').innerHTML=esc(s.label)+' <small>'+esc(s.sub)+'</small>';
  document.getElementById('fctitle').textContent=s.label+' 대시보드';}}
function dirCls(n){return n==null?'':(n>=0?'up':'down');}
function kc(lab,ic,icc,val,vcls,sub){return '<div class="kpi"><div class="top">'+
 '<span class="lab">'+lab+'</span><span class="ico '+(icc||'')+'">'+
 '<i class="fa-solid '+ic+'"></i></span></div><div class="val '+(vcls||'')+'">'+val+
 '</div><div class="sub">'+(sub||'')+'</div></div>';}
function chip(n,isPct){if(n==null)return '';var u=n>=0;
 var v=isPct?(Number(n).toFixed(2)+'%'):('$'+Number(Math.abs(n)).toLocaleString(undefined,{maximumFractionDigits:2}));
 return '<span class="chip '+(u?'up':'down')+'"><i class="fa-solid fa-caret-'+(u?'up':'down')+
 '"></i>'+(u?'+':'-')+v+'</span>';}
function renderKPI(a,au){
 var rp=a.total_return_pct;
 document.getElementById('kpi').innerHTML=
  kc('총 평가자산','fa-wallet','b',fmt(a.total_assets),'',
   '순투입 '+fmt(a.net_invested))+
  kc('총 수익률','fa-chart-pie',(rp>=0?'g':'r'),
   (rp==null?'—':(rp>=0?'+':'')+Number(rp).toFixed(2)+'%'),dirCls(rp),
   '총손익 '+chip(a.total_pnl,false))+
  kc('실현손익','fa-circle-check',(a.realized_pnl>=0?'g':'r'),
   (a.realized_pnl>=0?'+':'')+fmt(a.realized_pnl),dirCls(a.realized_pnl),'완료 싸이클 누적')+
  kc('미실현손익','fa-chart-line',(a.unrealized_pnl>=0?'g':'r'),
   (a.unrealized_pnl>=0?'+':'')+fmt(a.unrealized_pnl),dirCls(a.unrealized_pnl),
   '현금비중 '+(a.cash_ratio==null?'—':Number(a.cash_ratio).toFixed(1)+'%'))+
  '<div class="kpi auto'+(au&&au.running?'':' off')+'"><div class="pl"><i class="fa-solid '+
   (au&&au.running?'fa-play':'fa-pause')+'"></i></div><div><div class="lab">자동매매 상태</div>'+
   '<div class="av">'+(au&&au.running?'운영중':'정지')+'</div>'+
   '<div class="sub">'+((au&&au.active)||0)+' / '+((au&&au.total)||0)+' 전략 가동</div></div></div>';}
function renderStratTable(arr){
 var w=document.getElementById('pstrat');
 if(!arr||!arr.length){w.innerHTML='<div class="muted">데이터 없음</div>';return;}
 w.innerHTML='<table class="tbl"><thead><tr><th>전략</th>'+
  '<th style="text-align:right">투입금액</th><th style="text-align:right">누적손익</th>'+
  '<th style="text-align:right">수익률</th><th style="text-align:right">MDD</th>'+
  '<th style="text-align:right">승률</th><th>상태</th></tr></thead><tbody>'+
  arr.map(function(s){
   var st=s.kill_switch?'<span class="badge stop">정지</span>':'<span class="badge run">가동</span>';
   var md=s.mdd_pct==null?'<span class="muted" style="padding:0">수집중</span>':
    '<span class="neg">'+Number(s.mdd_pct).toFixed(2)+'%</span>';
   var wr=s.win_rate==null?'—':Number(s.win_rate).toFixed(1)+'%';
   return '<tr><td><b>'+esc(s.display_name)+'</b></td>'+
   '<td style="text-align:right">'+fmt(s.invested)+'</td>'+
   '<td style="text-align:right">'+sgn(s.realized_pnl)+'</td>'+
   '<td style="text-align:right">'+pctv(s.return_pct)+'</td>'+
   '<td style="text-align:right">'+md+'</td>'+
   '<td style="text-align:right">'+wr+'</td><td>'+st+'</td></tr>';}).join('')+'</tbody></table>';}
function renderHold(arr){
 var w=document.getElementById('phold');var rows=[];
 (arr||[]).forEach(function(s){(s.holdings||[]).forEach(function(h){
  rows.push({d:s.display_name,t:h.ticker,q:h.qty,a:h.avg_price,c:h.cost});});});
 if(!rows.length){w.innerHTML='<div class="muted">보유 종목 없음</div>';return;}
 w.innerHTML='<table class="tbl"><thead><tr><th>종목</th><th>전략</th>'+
  '<th style="text-align:right">수량</th><th style="text-align:right">평단가</th>'+
  '<th style="text-align:right">매입금액</th></tr></thead><tbody>'+
  rows.map(function(r){return '<tr><td><b>'+esc(r.t)+'</b></td><td>'+esc(r.d)+'</td>'+
  '<td style="text-align:right">'+r.q+'</td><td style="text-align:right">'+fmt(r.a)+'</td>'+
  '<td style="text-align:right">'+fmt(r.c)+'</td></tr>';}).join('')+'</tbody></table>';}
function renderTrades(ts){
 var w=document.getElementById('ptrade');
 if(!ts||!ts.length){w.innerHTML='<div class="muted">매매 내역 없음</div>';return;}
 w.innerHTML='<table class="tbl"><thead><tr><th>일자</th><th>전략</th><th>종목</th>'+
  '<th>구분</th><th style="text-align:right">수량</th><th style="text-align:right">체결가</th>'+
  '<th style="text-align:right">금액</th></tr></thead><tbody>'+
  ts.map(function(t){var sd=t.side==='buy'?'<span class="tag buy">매수</span>':'<span class="tag sell">매도</span>';
  return '<tr><td>'+esc(t.trade_date)+'</td><td>'+esc(t.display_name)+'</td><td><b>'+esc(t.ticker)+
  '</b></td><td>'+sd+'</td><td style="text-align:right">'+t.qty+
  '</td><td style="text-align:right">'+fmt(t.price)+'</td><td style="text-align:right">'+fmt(t.amount)+
  '</td></tr>';}).join('')+'</tbody></table>';}
function renderErrors(arr){
 var e=document.getElementById('elog');var rows=[];
 (arr||[]).forEach(function(s){(s.errors||[]).forEach(function(l){
  rows.push({d:s.display_name,lv:l.level,m:l.message,t:l.created_at});});});
 if(!rows.length){e.innerHTML='<div class="muted"><i class="fa-solid fa-circle-check" '+
  'style="color:#10b981"></i> 최근 주문/API 오류 없음</div>';return;}
 rows.sort(function(a,b){return (b.t||'').localeCompare(a.t||'');});
 e.innerHTML='<div class="elist">'+rows.slice(0,15).map(function(r){
  return '<div class="eitem"><span class="lvl '+esc(r.lv)+'">'+esc(r.lv)+'</span>'+
  '<span class="emsg">['+esc(r.d)+'] '+esc(r.m)+'</span>'+
  '<span class="etime">'+esc((r.t||'').replace('T',' ').slice(0,19))+'</span></div>';}).join('')+'</div>';}
function loadMetrics(){fetch('/api/suite/metrics').then(function(r){return r.json();})
 .then(function(d){renderKPI(d.account||{},d.automation||{});renderStratTable(d.strategies||[]);
  renderHold(d.strategies||[]);renderTrades(d.recent_trades||[]);renderErrors(d.strategies||[]);
  (d.strategies||[]).forEach(function(s,i){var dt=document.getElementById('dot'+i);
   if(dt)dt.className='dot '+(s.kill_switch?'off':'on');});
  document.getElementById('upd').innerHTML='마지막 업데이트<br>'+
   esc((d.generated_at||'').replace('T',' ').slice(0,19));
  var f=document.getElementById('foot');
  f.innerHTML='<b>공용 계좌</b> 69567573 · real<br>스냅샷 '+
   esc(((d.account&&d.account.snapshot_at)||'').replace('T',' ').slice(0,16));})
 .catch(function(){document.getElementById('pstrat').innerHTML='<div class="muted">지표 로드 실패</div>';});}
function days(r){return {'1M':30,'3M':90,'6M':180,'1Y':365,'전체':99999}[r]||180;}
function filt(pts,r){if(!pts.length)return pts;var n=days(r);
 var last=new Date(pts[pts.length-1].ts);var cut=new Date(last.getTime()-n*864e5);
 return pts.filter(function(p){return new Date(p.ts)>=cut;});}
function lbl(ts){return String(ts).slice(5,10).replace('-','/');}
var WRAP={ch1:'cw1',ch2:'cw2'};
function axes(pfx){return {responsive:true,maintainAspectRatio:false,
 interaction:{mode:'index',intersect:false},layout:{padding:{top:6}},
 plugins:{legend:{position:'bottom',labels:{usePointStyle:true,pointStyle:'circle',
  boxWidth:7,boxHeight:7,padding:16,font:{size:11.5,family:"'Noto Sans KR'"},color:'#6b7280'}},
  tooltip:{backgroundColor:'#111827',titleColor:'#fff',bodyColor:'#e5e7eb',padding:11,
   cornerRadius:9,boxPadding:5,titleFont:{size:11.5},bodyFont:{size:12},
   callbacks:{label:function(c){return ' '+c.dataset.label+': '+(pfx==='%'?
    Number(c.parsed.y).toFixed(2)+'%':'$'+Number(c.parsed.y).toLocaleString());}}}},
 scales:{x:{grid:{display:false},border:{display:false},
  ticks:{color:'#9aa1ad',font:{size:10.5},maxTicksLimit:7,maxRotation:0,padding:6}},
 y:{grid:{color:'#eef0f4'},border:{display:false},
  ticks:{color:'#9aa1ad',font:{size:10.5},padding:8,maxTicksLimit:6,
  callback:function(v){return pfx==='%'?v+'%':(Math.abs(v)>=1000?
   '$'+(v/1000).toFixed(0)+'k':'$'+v);}}}}};}
function setEmpty(id,title,sub){var w=document.getElementById(WRAP[id]);if(!w)return;
 w.innerHTML='<div class="empty"><i class="fa-solid fa-chart-area"></i>'+
 '<div class="t">'+title+'</div><div class="s">'+sub+'</div></div>';}
function freshCanvas(id){var w=document.getElementById(WRAP[id]);
 w.innerHTML='<canvas id="'+id+'"></canvas>';return document.getElementById(id);}
function area(cv,hex,rgb){var g=cv.getContext('2d').createLinearGradient(0,0,0,300);
 g.addColorStop(0,'rgba('+rgb+',.20)');g.addColorStop(1,'rgba('+rgb+',0)');return g;}
function drawCharts(){if(!SER)return;
 if(SER.collecting||!SER.points||SER.points.length<2){
  setEmpty('ch1','자산추이 데이터 수집중','equity 스냅샷이 30분 주기로 누적되면 자동 표시됩니다');
  setEmpty('ch2','전략별 수익률 데이터 수집중','스냅샷 2포인트 이상 누적 후 표시');return;}
 if(CH1){CH1.destroy();CH1=null;}if(CH2){CH2.destroy();CH2=null;}
 var c1=freshCanvas('ch1');
 var p1=filt(SER.points,R1);var L1=p1.map(function(p){return lbl(p.ts);});
 CH1=new Chart(c1,{type:'line',data:{labels:L1,datasets:[
  {label:'평가자산',data:p1.map(function(p){return p.total_assets;}),borderColor:'#4f86f7',
   backgroundColor:area(c1,'#4f86f7','79,134,247'),borderWidth:2.2,pointRadius:0,
   fill:true,tension:.35},
  {label:'순투입금액',data:p1.map(function(p){return p.net_invested;}),borderColor:'#f59e0b',
   borderWidth:2,pointRadius:0,borderDash:[5,4],tension:.35},
  {label:'누적손익',data:p1.map(function(p){return p.cum_pnl;}),borderColor:'#10b981',
   borderWidth:2,pointRadius:0,tension:.35}]},options:axes('$')});
 var c2=freshCanvas('ch2');
 var p2=filt(SER.points,R2);var L2=p2.map(function(p){return lbl(p.ts);});
 var ds=[];var i=0;for(var k in (SER.strategy_return||{})){
  var nm=(STRATS.filter(function(s){return s.key===k;})[0]||{}).label||k;
  var full=SER.strategy_return[k];var off=SER.points.length-p2.length;
  ds.push({label:nm,data:full.slice(off),borderColor:PAL[i%PAL.length],
   borderWidth:2.2,pointRadius:0,tension:.35});i++;}
 CH2=new Chart(c2,{type:'line',data:{labels:L2,datasets:ds},options:axes('%')});}
function loadSeries(){fetch('/api/suite/series').then(function(r){return r.json();})
 .then(function(d){SER=d;drawCharts();}).catch(function(){
  setEmpty('ch1','자산추이 로드 실패','잠시 후 새로고침');
  setEmpty('ch2','수익률 로드 실패','잠시 후 새로고침');});}
function buildSeg(id,cur,cb){var box=document.getElementById(id);
 ['1M','3M','6M','1Y','전체'].forEach(function(r){var b=document.createElement('button');
  b.className='segb'+(r===cur?' on':'');b.textContent=r;b.onclick=function(){
   [].forEach.call(box.children,function(x){x.classList.remove('on');});
   b.classList.add('on');cb(r);};box.appendChild(b);});}
function refreshAll(){if(view==='ov'){loadMetrics();loadSeries();}
 else{var f=document.getElementById('frame');if(f.src)f.src=f.src;}}
function closeSb(){document.getElementById('sb').classList.remove('open');
 document.getElementById('scrim').classList.remove('show');}
document.getElementById('hamb').onclick=function(){
 document.getElementById('sb').classList.toggle('open');
 document.getElementById('scrim').classList.toggle('show');};
document.getElementById('scrim').onclick=closeSb;
document.getElementById('refresh').onclick=refreshAll;
buildSeg('seg1',R1,function(r){R1=r;drawCharts();});
buildSeg('seg2',R2,function(r){R2=r;drawCharts();});
buildNav();pick(0);
setInterval(function(){if(view==='ov'){loadMetrics();loadSeries();}},60000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    """통합 포털 셸: 클린 라이트 디자인. 좌측 전략 네비 + 공용 계좌/예산 KPI + 전략 대시보드 iframe."""
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
    strat_js = _json.dumps(strategies, ensure_ascii=False)
    html = _SHELL_HTML.replace("__STRATS__", strat_js)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
