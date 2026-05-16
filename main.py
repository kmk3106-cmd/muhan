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
            sched.add_job(_equity_snapshot, "date",
                          run_date=datetime.now() + timedelta(seconds=60),
                          id="equity_snapshot_boot")
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
    """통합 대시보드 15개 지표 (읽기 전용 집계, 신규 KIS 호출 없음)."""
    from core.suite_metrics import build_metrics
    return build_metrics()


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
<style>
:root{--primary:#4f86f7;--primary-dark:#3a6fd8;--purple:#6c5ce7;--success:#10b981;
--warning:#f59e0b;--danger:#ef4444;--gray-50:#f9fafb;--gray-100:#f3f4f6;--gray-200:#e5e7eb;
--gray-300:#d1d5db;--gray-400:#9ca3af;--gray-500:#6b7280;--gray-600:#4b5563;--gray-700:#374151;
--gray-800:#1f2937;--gray-900:#111827;--sidebar-width:260px;--header-height:64px}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:'Noto Sans KR',sans-serif;background:var(--gray-50);color:var(--gray-800)}
.app-layout{display:flex;min-height:100vh}
.sidebar{width:var(--sidebar-width);background:#fff;border-right:1px solid var(--gray-200);
display:flex;flex-direction:column;position:fixed;top:0;bottom:0;left:0;z-index:30}
.sidebar-brand{background:var(--gray-900);color:#fff;padding:0 20px;height:var(--header-height);
display:flex;align-items:center;gap:11px;flex-shrink:0}
.sidebar-brand .logo{width:30px;height:30px;border-radius:8px;background:var(--primary);
display:flex;align-items:center;justify-content:center;font-size:15px}
.sidebar-brand b{font-size:15px;font-weight:700;letter-spacing:.2px}
.sidebar-brand small{display:block;font-size:11px;color:var(--gray-400);font-weight:400}
.nav-section-title{padding:18px 20px 8px;font-size:11px;font-weight:600;color:var(--gray-400);
letter-spacing:.06em;text-transform:uppercase}
.sidebar-nav{flex:1;padding:0 10px;overflow-y:auto}
.nav-item{display:flex;align-items:center;gap:12px;width:100%;text-align:left;border:none;
background:none;cursor:pointer;padding:11px 12px;border-radius:9px;color:var(--gray-600);
font-family:inherit;font-size:13.5px;margin-bottom:3px;transition:background .15s,color .15s}
.nav-item:hover{background:var(--gray-100);color:var(--gray-900)}
.nav-item.active{background:var(--gray-900);color:#fff}
.nav-item .ic{width:20px;text-align:center;font-size:15px;flex-shrink:0}
.nav-item .tx{flex:1;min-width:0}
.nav-item .tx b{font-weight:600;display:block}
.nav-item .tx span{font-size:11px;color:var(--gray-400);font-weight:400}
.nav-item.active .tx span{color:var(--gray-300)}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--gray-300)}
.dot.on{background:var(--success)}.dot.off{background:var(--danger)}
.sidebar-foot{border-top:1px solid var(--gray-200);padding:14px 18px;font-size:11.5px;
color:var(--gray-500);line-height:1.7}
.sidebar-foot b{color:var(--gray-700)}
.main{flex:1;margin-left:var(--sidebar-width);display:flex;flex-direction:column;min-width:0}
.topbar{height:var(--header-height);background:#fff;border-bottom:1px solid var(--gray-200);
display:flex;align-items:center;gap:14px;padding:0 26px;position:sticky;top:0;z-index:20}
.hamb{display:none;border:none;background:none;font-size:18px;color:var(--gray-700);cursor:pointer}
.page-title{font-size:19px;font-weight:700;color:var(--gray-900);flex:1}
.page-title small{font-size:13px;font-weight:400;color:var(--gray-400);margin-left:10px}
.btn{font-family:inherit;padding:9px 16px;border-radius:9px;font-size:13px;font-weight:500;
cursor:pointer;border:none;display:inline-flex;align-items:center;gap:7px}
.btn-secondary{background:var(--gray-100);color:var(--gray-700);border:1px solid var(--gray-200)}
.btn-secondary:hover{background:var(--gray-200)}
.content{padding:22px 26px;display:flex;flex-direction:column;gap:18px}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}
.kpi{background:#fff;border:1px solid var(--gray-200);border-radius:12px;padding:18px 20px}
.kpi .lab{font-size:12px;color:var(--gray-500);display:flex;align-items:center;gap:7px}
.kpi .lab i{color:var(--gray-400)}
.kpi .val{font-size:22px;font-weight:700;color:var(--gray-900);margin-top:8px}
.kpi .sub{font-size:12px;margin-top:5px;color:var(--gray-400)}
.kpi .up{color:var(--success)}.kpi .down{color:var(--danger)}
.card{background:#fff;border:1px solid var(--gray-200);border-radius:12px;overflow:hidden}
.card-header{padding:15px 22px;border-bottom:1px solid var(--gray-200);display:flex;
align-items:center;gap:10px}
.card-header .ct{font-size:15px;font-weight:600;color:var(--gray-900);flex:1}
.badge{padding:3px 10px;border-radius:20px;font-size:11.5px;font-weight:600}
.badge.run{background:#ecfdf5;color:#047857}.badge.stop{background:#fef2f2;color:#b91c1c}
.frame-card{flex:1;display:flex;flex-direction:column;min-height:560px}
iframe{flex:1;width:100%;border:0;background:#fff;min-height:560px}
.bgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;padding:18px 22px}
.bcell{border:1px solid var(--gray-200);border-radius:10px;padding:14px 16px}
.bcell .bn{font-weight:600;color:var(--gray-900);font-size:13.5px;display:flex;
align-items:center;justify-content:space-between}
.bcell .br{font-size:12px;color:var(--gray-500);margin-top:8px;line-height:1.8}
.bcell .br b{color:var(--gray-800)}
.ov{color:var(--danger);font-weight:600}
.scrim{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:25}
@media(max-width:980px){
.sidebar{transform:translateX(-100%);transition:transform .25s}
.sidebar.open{transform:none}.main{margin-left:0}.hamb{display:block}
.scrim.show{display:block}}
.sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px;padding:18px 22px}
.scard{border:1px solid var(--gray-200);border-radius:10px;padding:16px 18px}
.scard .sh{display:flex;align-items:center;gap:8px;font-weight:600;color:var(--gray-900);font-size:14px}
.scard .sh .badge{margin-left:auto}
.mrow{display:flex;flex-wrap:wrap;gap:16px;margin-top:13px}
.metric{min-width:80px}.metric .ml{font-size:11px;color:var(--gray-500)}
.metric .mv{font-size:16px;font-weight:700;color:var(--gray-900);margin-top:3px}
.pos{color:var(--success)}.neg{color:var(--danger)}
.hold{margin-top:13px;border-top:1px dashed var(--gray-200);padding-top:10px;font-size:12px}
.hold table{width:100%;border-collapse:collapse}.hold td{padding:3px 0;color:var(--gray-600)}
.hold td:last-child{text-align:right}
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th{text-align:left;color:var(--gray-500);font-weight:600;padding:9px 22px;border-bottom:1px solid var(--gray-200);font-size:11.5px}
.tbl td{padding:9px 22px;border-bottom:1px solid var(--gray-100);color:var(--gray-700)}
.tbl tr:last-child td{border-bottom:0}
.tag{padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600}
.tag.buy{background:#eff6ff;color:#1d4ed8}.tag.sell{background:#fef2f2;color:#b91c1c}
.elist{padding:8px 22px 16px}
.eitem{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--gray-100);font-size:12.5px}
.eitem:last-child{border-bottom:0}
.lvl{padding:1px 8px;border-radius:5px;font-size:10.5px;font-weight:700;height:fit-content}
.lvl.ERROR{background:#fef2f2;color:#b91c1c}.lvl.WARNING{background:#fffbeb;color:#b45309}
.emsg{flex:1;color:var(--gray-700);word-break:break-all}
.etime{color:var(--gray-400);white-space:nowrap;font-size:11px}
.muted{color:var(--gray-400);font-size:12.5px;padding:16px 22px}
.hidden{display:none!important}
</style></head><body>
<div class="app-layout">
  <aside class="sidebar" id="sb">
    <div class="sidebar-brand">
      <div class="logo"><i class="fa-solid fa-layer-group"></i></div>
      <div><b>trading_suite</b><small>멀티전략 운영 포털</small></div>
    </div>
    <div class="nav-section-title">메뉴</div>
    <nav class="sidebar-nav" id="nav"></nav>
    <div class="sidebar-foot" id="foot">계좌 정보 불러오는 중…</div>
  </aside>
  <div class="scrim" id="scrim"></div>
  <main class="main">
    <header class="topbar">
      <button class="hamb" id="hamb"><i class="fa-solid fa-bars"></i></button>
      <div class="page-title" id="ptitle">—</div>
      <button class="btn btn-secondary" id="refresh"><i class="fa-solid fa-rotate"></i> 새로고침</button>
    </header>
    <div class="content">
      <section id="overview">
        <div class="kpi-grid" id="kpi"></div>
        <div class="card" style="margin-top:18px">
          <div class="card-header"><span class="ct">전략 성과</span>
            <span style="font-size:12px;color:#9ca3af">실현손익 기준 · 보유는 평단·원가</span></div>
          <div class="sgrid" id="sgrid"><div class="muted">불러오는 중…</div></div>
        </div>
        <div class="card" style="margin-top:18px">
          <div class="card-header"><span class="ct">최근 매매로그</span></div>
          <div id="tlogwrap"><div class="muted">불러오는 중…</div></div>
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
var STRATS=__STRATS__;var view='ov';var curi=0;
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
 var items=[{ov:1,label:'통합 현황',sub:'계좌·전략·로그·오류',icon:'fa-gauge-high'}]
  .concat(STRATS.map(function(s){return {ov:0,label:s.label,sub:s.sub,icon:s.icon};}));
 items.forEach(function(it,idx){
  var b=document.createElement('button');b.className='nav-item'+(idx===0?' active':'');
  b.dataset.idx=idx;
  b.innerHTML='<span class="ic"><i class="fa-solid '+it.icon+'"></i></span>'+
  '<span class="tx"><b>'+it.label+'</b><span>'+it.sub+'</span></span>'+
  (it.ov?'':'<span class="dot" id="dot'+(idx-1)+'"></span>');
  b.onclick=function(){pick(+this.dataset.idx);};nav.appendChild(b);});}
function setActive(idx){[].forEach.call(nav.children,function(b,j){
 b.className='nav-item'+(j===idx?' active':'');});}
function pick(idx){setActive(idx);closeSb();
 if(idx===0){view='ov';curi=0;
  document.getElementById('overview').classList.remove('hidden');
  document.getElementById('stratview').classList.add('hidden');
  document.getElementById('ptitle').innerHTML='통합 현황 <small>공용계좌 69567573 · 멀티전략</small>';
  loadMetrics();
 }else{view='st';curi=idx-1;var s=STRATS[curi];
  document.getElementById('overview').classList.add('hidden');
  document.getElementById('stratview').classList.remove('hidden');
  var f=document.getElementById('frame');if(f.src.indexOf(s.path)<0)f.src=s.path;
  document.getElementById('ptitle').innerHTML=s.label+' <small>'+s.sub+'</small>';
  document.getElementById('fctitle').textContent=s.label+' 대시보드';}}
function kpiCard(lab,ic,val,sub,cls){return '<div class="kpi"><div class="lab"><i class="fa-solid '+
 ic+'"></i>'+lab+'</div><div class="val">'+val+'</div><div class="sub '+(cls||'')+'">'+(sub||'')+'</div></div>';}
function dirCls(n){return n==null?'':(n>=0?'up':'down');}
function renderKPI(a){
 document.getElementById('kpi').innerHTML=
  kpiCard('총 평가자산','fa-wallet',fmt(a.total_assets),'공용계좌 스냅샷')+
  kpiCard('순투입(매입)','fa-money-bill-transfer',fmt(a.net_invested),'')+
  kpiCard('총손익','fa-scale-balanced',(a.total_pnl>=0?'+':'')+fmt(a.total_pnl),
   (a.total_return_pct==null?'':(a.total_return_pct>=0?'+':'')+Number(a.total_return_pct).toFixed(2)+'%'),dirCls(a.total_pnl))+
  kpiCard('총수익률','fa-percent',(a.total_return_pct==null?'—':(a.total_return_pct>=0?'+':'')+Number(a.total_return_pct).toFixed(2)+'%'),'',dirCls(a.total_return_pct))+
  kpiCard('금일손익','fa-calendar-day',(a.today_pnl==null?'<span style="color:#9ca3af">수집중</span>':(a.today_pnl>=0?'+':'')+fmt(a.today_pnl)),'',dirCls(a.today_pnl))+
  kpiCard('실현손익','fa-circle-check',(a.realized_pnl>=0?'+':'')+fmt(a.realized_pnl),'완료 싸이클 Σ',dirCls(a.realized_pnl))+
  kpiCard('미실현손익','fa-chart-line',(a.unrealized_pnl>=0?'+':'')+fmt(a.unrealized_pnl),'평가손익',dirCls(a.unrealized_pnl))+
  kpiCard('현금비중','fa-coins',(a.cash_ratio==null?'—':Number(a.cash_ratio).toFixed(1)+'%'),'현금 '+fmt(a.cash))+
  kpiCard('계좌 MDD','fa-arrow-trend-down',(a.mdd_pct==null?'<span style="color:#9ca3af">수집중</span>':Number(a.mdd_pct).toFixed(2)+'%'),'최대낙폭','down');}
function renderStrats(arr){
 var g=document.getElementById('sgrid');
 if(!arr||!arr.length){g.innerHTML='<div class="muted">전략 데이터 없음</div>';return;}
 g.innerHTML=arr.map(function(s){
  var bd=s.kill_switch?'<span class="badge stop">정지</span>':'<span class="badge run">가동</span>';
  var hold='';
  if(s.holdings&&s.holdings.length){hold='<div class="hold"><table>'+
   s.holdings.map(function(h){return '<tr><td><b>'+esc(h.ticker)+'</b></td><td>'+
   h.qty+'주 @ '+fmt(h.avg_price)+'</td><td>원가 '+fmt(h.cost)+'</td></tr>';}).join('')+
   '</table></div>';}
  else{hold='<div class="hold" style="color:#9ca3af">보유 종목 없음</div>';}
  return '<div class="scard"><div class="sh"><i class="fa-solid fa-circle" style="font-size:8px;color:'+
   (s.kill_switch?'#ef4444':'#10b981')+'"></i>'+esc(s.display_name)+bd+'</div>'+
   '<div class="mrow">'+
   '<div class="metric"><div class="ml">수익률</div><div class="mv">'+pctv(s.return_pct)+'</div></div>'+
   '<div class="metric"><div class="ml">누적손익</div><div class="mv">'+sgn(s.realized_pnl)+'</div></div>'+
   '<div class="metric"><div class="ml">MDD</div><div class="mv">'+
    (s.mdd_pct==null?'<span class="muted" style="padding:0">수집중</span>':'<span class="neg">'+Number(s.mdd_pct).toFixed(2)+'%</span>')+'</div></div>'+
   '<div class="metric"><div class="ml">보유종목</div><div class="mv">'+s.holdings_count+'</div></div>'+
   '<div class="metric"><div class="ml">싸이클</div><div class="mv">'+s.cycles+'</div></div>'+
   '</div>'+hold+'</div>';}).join('');}
function renderTrades(ts){
 var w=document.getElementById('tlogwrap');
 if(!ts||!ts.length){w.innerHTML='<div class="muted">매매 내역 없음</div>';return;}
 w.innerHTML='<table class="tbl"><thead><tr><th>일자</th><th>전략</th><th>종목</th>'+
  '<th>구분</th><th>유형</th><th style="text-align:right">수량</th>'+
  '<th style="text-align:right">가격</th><th style="text-align:right">금액</th></tr></thead><tbody>'+
  ts.map(function(t){var sd=t.side==='buy'?'<span class="tag buy">매수</span>':'<span class="tag sell">매도</span>';
  return '<tr><td>'+esc(t.trade_date)+'</td><td>'+esc(t.display_name)+'</td><td><b>'+esc(t.ticker)+
  '</b></td><td>'+sd+'</td><td>'+esc(t.order_type)+'</td><td style="text-align:right">'+t.qty+
  '</td><td style="text-align:right">'+fmt(t.price)+'</td><td style="text-align:right">'+fmt(t.amount)+
  '</td></tr>';}).join('')+'</tbody></table>';}
function renderErrors(arr){
 var e=document.getElementById('elog');var rows=[];
 (arr||[]).forEach(function(s){(s.errors||[]).forEach(function(l){
  rows.push({d:s.display_name,lv:l.level,m:l.message,t:l.created_at});});});
 if(!rows.length){e.innerHTML='<div class="muted"><i class="fa-solid fa-circle-check" '+
  'style="color:#10b981"></i> 최근 주문/API 오류 없음</div>';return;}
 rows.sort(function(a,b){return (b.t||'').localeCompare(a.t||'');});
 e.innerHTML='<div class="elist">'+rows.slice(0,20).map(function(r){
  return '<div class="eitem"><span class="lvl '+esc(r.lv)+'">'+esc(r.lv)+'</span>'+
  '<span class="emsg">['+esc(r.d)+'] '+esc(r.m)+'</span>'+
  '<span class="etime">'+esc((r.t||'').replace('T',' ').slice(0,19))+'</span></div>';}).join('')+'</div>';}
function loadMetrics(){fetch('/api/suite/metrics').then(function(r){return r.json();})
 .then(function(d){renderKPI(d.account||{});renderStrats(d.strategies||[]);
  renderTrades(d.recent_trades||[]);renderErrors(d.strategies||[]);
  (d.strategies||[]).forEach(function(s,i){var dt=document.getElementById('dot'+i);
   if(dt)dt.className='dot '+(s.kill_switch?'off':'on');});
  var f=document.getElementById('foot');
  f.innerHTML='<b>공용 계좌</b> 69567573 · real<br>스냅샷 '+
   esc((d.account&&d.account.snapshot_at||'').replace('T',' ').slice(0,16))+
   '<br>갱신 '+esc((d.generated_at||'').replace('T',' ').slice(11,19));})
 .catch(function(){document.getElementById('sgrid').innerHTML=
  '<div class="muted">지표 로드 실패</div>';});}
function refreshAll(){if(view==='ov'){loadMetrics();}
 else{var f=document.getElementById('frame');if(f.src)f.src=f.src;}}
function closeSb(){document.getElementById('sb').classList.remove('open');
 document.getElementById('scrim').classList.remove('show');}
document.getElementById('hamb').onclick=function(){
 document.getElementById('sb').classList.toggle('open');
 document.getElementById('scrim').classList.toggle('show');};
document.getElementById('scrim').onclick=closeSb;
document.getElementById('refresh').onclick=refreshAll;
buildNav();pick(0);
setInterval(function(){if(view==='ov')loadMetrics();},60000);
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
