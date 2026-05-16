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


@app.get("/", response_class=HTMLResponse)
def root():
    """통합 셸: 4전략 확장형 탭 + 공용 계좌/예산 요약 + 전략 대시보드 iframe."""
    from core.strategy_adapters import DISPLAY_NAMES
    # 데이터 주도: SUB_APPS 순서 = 탭 순서. 신규 전략은 SUB_APPS 등록만 하면 탭 자동 확장.
    strategies = [
        {"key": k, "label": DISPLAY_NAMES.get(k, k), "path": f"/{k}/dashboard"}
        for k in SUB_APPS
    ]
    import json as _json
    strat_js = _json.dumps(strategies, ensure_ascii=False)
    html = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>trading_suite 통합 대시보드</title>
<style>
*{box-sizing:border-box}html,body{margin:0;height:100%;background:#0e1117;color:#e6edf3;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Malgun Gothic",sans-serif}
#top{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 12px;
background:#161b22;border-bottom:1px solid #30363d}
#top h1{font-size:15px;margin:0 12px 0 0;font-weight:700;white-space:nowrap}
.tab{padding:7px 14px;border:1px solid #30363d;border-radius:7px;background:#0e1117;
color:#9da7b3;cursor:pointer;font-size:13px;white-space:nowrap}
.tab.active{background:#1f6feb;color:#fff;border-color:#1f6feb}
#budget{margin-left:auto;display:flex;flex-wrap:wrap;gap:6px;font-size:11px}
.chip{background:#0e1117;border:1px solid #30363d;border-radius:6px;padding:4px 8px;color:#9da7b3}
.chip b{color:#e6edf3}.chip .ov{color:#f85149}
#frameWrap{position:absolute;top:auto;left:0;right:0;bottom:0;width:100%}
iframe{display:block;width:100%;height:calc(100vh - 49px);border:0;background:#0e1117}
@media(max-width:640px){#top h1{width:100%;margin:0 0 4px}#budget{margin-left:0;width:100%}
iframe{height:calc(100vh - 92px)}}
</style></head><body>
<div id="top">
  <h1>trading_suite</h1>
  <div id="tabs"></div>
  <div id="budget">불러오는 중…</div>
</div>
<div id="frameWrap"><iframe id="frame" title="strategy"></iframe></div>
<script>
var STRATS=__STRATS__;var frame=document.getElementById("frame");
var tabs=document.getElementById("tabs");
function sel(i){frame.src=STRATS[i].path;
[].forEach.call(tabs.children,function(b,j){b.className="tab"+(j===i?" active":"");});}
STRATS.forEach(function(s,i){var b=document.createElement("button");
b.className="tab";b.textContent=s.label;b.onclick=function(){sel(i);};tabs.appendChild(b);});
if(STRATS.length)sel(0);
function fmt(n){return n==null?"—":"$"+Number(n).toLocaleString();}
function loadBudget(){fetch("/api/suite/strategies").then(function(r){return r.json();})
.then(function(d){var el=document.getElementById("budget");el.innerHTML="";
(d.budgets||[]).forEach(function(b){var c=document.createElement("span");c.className="chip";
var rem=b.over_budget?("<span class=ov>초과</span>"):("잔여 "+fmt(b.remaining));
c.innerHTML="<b>"+b.display_name+"</b> 사용 "+fmt(b.used)+" / 할당 "+fmt(b.assigned_total)
+" · "+rem+" · 종목 "+b.ticker_count;el.appendChild(c);});})
.catch(function(){document.getElementById("budget").textContent="요약 로드 실패";});}
loadBudget();setInterval(loadBudget,60000);
</script></body></html>""".replace("__STRATS__", strat_js)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
