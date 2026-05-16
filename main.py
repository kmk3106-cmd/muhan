# -*- coding: utf-8 -*-
"""trading_suite 부모 FastAPI - 두 전략 sub-app 마운트 + lifespan 통합.

단일 프로세스·단일 포트(8000)에서 무한매수법/떨사오팔을 함께 운용한다.
각 전략의 스케줄러·웹소켓은 sub-app 자체 lifespan에서 기동되는데,
Starlette는 마운트된 sub-app의 lifespan을 자동 실행하지 않으므로
부모 lifespan에서 각 sub-app lifespan을 수동으로 진입/정리한다.
"""
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

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


@app.get("/", response_class=HTMLResponse)
def root():
    # P5에서 통합 대시보드 셸(4전략 확장형 탭)로 교체. 현재는 임시 진입점.
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'>"
        "<h2>trading_suite</h2><ul>"
        "<li><a href='/infinite/dashboard'>무한매수법</a></li>"
        "<li><a href='/ddsop/dashboard'>떨사오팔</a></li>"
        "</ul>"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
