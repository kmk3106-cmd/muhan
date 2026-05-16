# -*- coding: utf-8 -*-
"""trading_suite 부모 FastAPI - 두 전략 sub-app 마운트 + lifespan 통합.

단일 프로세스·단일 포트(8000)에서 무한매수법/떨사오팔을 함께 운용한다.
각 전략의 스케줄러·웹소켓은 sub-app 자체 lifespan에서 기동되는데,
Starlette는 마운트된 sub-app의 lifespan을 자동 실행하지 않으므로
부모 lifespan에서 각 sub-app lifespan을 수동으로 진입/정리한다.
"""
import logging
from contextlib import AsyncExitStack, asynccontextmanager

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
        logger.info("[suite] 전체 기동 완료 (port 8000)")
        yield
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
</style></head><body>
<div class="app-layout">
  <aside class="sidebar" id="sb">
    <div class="sidebar-brand">
      <div class="logo"><i class="fa-solid fa-layer-group"></i></div>
      <div><b>trading_suite</b><small>멀티전략 운영 포털</small></div>
    </div>
    <div class="nav-section-title">전략</div>
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
      <div class="kpi-grid" id="kpi"></div>
      <div class="card">
        <div class="card-header"><span class="ct">전략별 시드 가드레일</span></div>
        <div class="bgrid" id="bgrid"></div>
      </div>
      <div class="card frame-card">
        <div class="card-header"><span class="ct" id="fctitle">전략 대시보드</span>
          <span class="badge" id="fcbadge"></span></div>
        <iframe id="frame" title="strategy dashboard"></iframe>
      </div>
    </div>
  </main>
</div>
<script>
var STRATS=__STRATS__;var cur=0;
var nav=document.getElementById('nav');
function fmt(n){return (n==null||n==='')?'—':'$'+Number(n).toLocaleString(undefined,{maximumFractionDigits:2});}
function pct(n){return (n==null)?'':((n>=0?'+':'')+Number(n).toFixed(2)+'%');}
function build(){STRATS.forEach(function(s,i){
 var b=document.createElement('button');b.className='nav-item'+(i===0?' active':'');b.dataset.i=i;
 b.innerHTML='<span class="ic"><i class="fa-solid '+s.icon+'"></i></span>'+
 '<span class="tx"><b>'+s.label+'</b><span>'+s.sub+'</span></span><span class="dot" id="dot'+i+'"></span>';
 b.onclick=function(){sel(+this.dataset.i);};nav.appendChild(b);});}
function sel(i){cur=i;var s=STRATS[i];
 document.getElementById('frame').src=s.path;
 document.getElementById('ptitle').innerHTML=s.label+' <small>'+s.sub+'</small>';
 document.getElementById('fctitle').textContent=s.label+' 대시보드';
 [].forEach.call(nav.children,function(b,j){b.className='nav-item'+(j===i?' active':'');});
 closeSb();refreshStatus();}
function kpiCard(lab,ic,val,sub,cls){return '<div class="kpi"><div class="lab"><i class="fa-solid '+
 ic+'"></i>'+lab+'</div><div class="val">'+val+'</div><div class="sub '+(cls||'')+'">'+(sub||'')+'</div></div>';}
function loadAccount(){var k=STRATS[0].key;
 fetch('/'+k+'/api/account_summary').then(function(r){return r.json();}).then(function(a){
  var dir=(a.pnl||0)>=0?'up':'down';
  document.getElementById('kpi').innerHTML=
   kpiCard('총 평가금액','fa-wallet',fmt(a.tot_evlu),'공용 계좌 69567573')+
   kpiCard('예수금(현금)','fa-coins',fmt(a.cash),'주문가능 현금')+
   kpiCard('주식 평가','fa-chart-pie',fmt(a.stock_evlu),'매입 '+fmt(a.buy_amt))+
   kpiCard('평가손익','fa-arrow-trend-up',fmt(a.pnl),pct(a.pnl_rt),dir);
 }).catch(function(){document.getElementById('kpi').innerHTML=
   kpiCard('계좌','fa-wallet','—','요약 로드 실패');});}
function loadBudget(){fetch('/api/suite/strategies').then(function(r){return r.json();})
 .then(function(d){var g=document.getElementById('bgrid');g.innerHTML='';
  (d.budgets||[]).forEach(function(b){var c=document.createElement('div');c.className='bcell';
   var rem=b.over_budget?'<span class="ov">예산 초과</span>':('잔여 <b>'+fmt(b.remaining)+'</b>');
   c.innerHTML='<div class="bn">'+b.display_name+'<span style="font-size:11px;color:#9ca3af">종목 '+
   b.ticker_count+'</span></div><div class="br">사용 <b>'+fmt(b.used)+'</b> / 할당 <b>'+
   fmt(b.assigned_total)+'</b><br>'+rem+'</div>';g.appendChild(c);});})
 .catch(function(){document.getElementById('bgrid').innerHTML=
   '<div class="bcell">요약 로드 실패</div>';});}
function refreshStatus(){STRATS.forEach(function(s,i){
 fetch('/'+s.key+'/api/status?check_api=false').then(function(r){return r.json();})
 .then(function(st){var d=document.getElementById('dot'+i);
  if(d)d.className='dot '+(st.kill_switch?'off':'on');
  if(i===cur){var bd=document.getElementById('fcbadge');
   bd.className='badge '+(st.kill_switch?'stop':'run');
   bd.textContent=st.kill_switch?'정지(Kill Switch)':('가동 · 다음 '+(st.next_worker_run||''));}})
 .catch(function(){});});}
function refreshAll(){loadAccount();loadBudget();refreshStatus();
 var f=document.getElementById('frame');if(f.src)f.src=f.src;}
function closeSb(){document.getElementById('sb').classList.remove('open');
 document.getElementById('scrim').classList.remove('show');}
document.getElementById('hamb').onclick=function(){
 document.getElementById('sb').classList.toggle('open');
 document.getElementById('scrim').classList.toggle('show');};
document.getElementById('scrim').onclick=closeSb;
document.getElementById('refresh').onclick=refreshAll;
build();
fetch('/'+STRATS[0].key+'/api/status?check_api=false').then(function(r){return r.json();})
 .then(function(st){document.getElementById('foot').innerHTML=
  '<b>공용 계좌</b> 69567573<br>모드 '+(st.kill_switch!=null?'real':'—')+
  ' · 서버 '+(st.server_time_kst||'').replace(' KST','')+' KST';}).catch(function(){});
sel(0);loadAccount();loadBudget();
setInterval(function(){loadAccount();loadBudget();refreshStatus();},60000);
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
