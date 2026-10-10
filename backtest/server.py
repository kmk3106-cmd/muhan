# -*- coding: utf-8 -*-
"""백테스트 웹 UI (로컬 전용, 조회만).

실행:
    python -m backtest.server            # http://127.0.0.1:8777
    python -m backtest.server --port 9000

운영 대시보드(main.py)와 완전히 분리된 별도 앱이다. 매매 코드는 import 만 한다.
기본값은 127.0.0.1 바인딩 — 외부에 열지 않는다.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import data as D                                  # noqa: E402
from backtest.engine import (Params, PRESETS, VR_PRESETS, STRATEGIES,  # noqa: E402
                             run, vr_default_pool_pct)

app = FastAPI(title="trading_suite backtest", docs_url=None, redoc_url=None)


class RunReq(BaseModel):
    strategy: str = "infinite"
    ticker: str = "TQQQ"
    start: str = ""
    end: str = ""
    seed: float = 5000.0
    fee_pct: float = 0.0
    A: int = 40
    R: float = 10.0
    compound: bool = False
    num_tranches: int = 7
    x_pct: float = 2.7
    loss_cut_days: int = 40
    seed_reflect: bool = False
    v4_compound: bool = False
    vr_unit: int = 4
    vr_g: float = 16.0
    vr_buy_limit_pct: float = 25.0
    vr_sell_steps: int = 11
    vr_cashflow: float = 0.0
    vr_pool_pct: float = -1.0
    vr_g_step_weeks: int = 26


class DlReq(BaseModel):
    ticker: str


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "ui.html").read_text(encoding="utf-8")


@app.get("/api/meta")
def meta():
    cached = D.cached_tickers()
    end = "2026-10-10"
    try:
        end = D.available_end() or end
    except Exception:
        pass                              # 키 없거나 오프라인이면 캐시만으로 동작
    # 캐시 범위 안에서 실제 사용 가능한 마지막 날짜
    if cached:
        try:
            rows = D.raw(cached[0], allow_download=False)
            end = min(end, rows[-1]["date"]) if rows else end
        except Exception:
            pass
    y = int(end[:4])
    quick = [
        {"label": "전체", "start": D.DATA_START, "end": end},
        {"label": "최근 5년", "start": f"{y-5}-{end[5:]}", "end": end},
        {"label": "최근 3년", "start": f"{y-3}-{end[5:]}", "end": end},
        {"label": "최근 1년", "start": f"{y-1}-{end[5:]}", "end": end},
        {"label": "2022 베어장", "start": "2022-01-01", "end": "2022-12-31"},
        {"label": "2020 코로나", "start": "2020-02-01", "end": "2020-06-30"},
        {"label": "2018 Q4", "start": "2018-09-01", "end": "2019-03-31"},
    ]
    return {"strategies": STRATEGIES, "presets": PRESETS,
            "vr_presets": VR_PRESETS, "cached": cached,
            "data_start": D.DATA_START, "data_end": end, "quick": quick}


@app.post("/api/run")
def api_run(q: RunReq):
    try:
        rows, dmeta = D.load(q.ticker.upper(), q.start or None, q.end or None,
                             allow_download=False)
    except D.DataError as e:
        # 캐시에 없으면 비용을 먼저 보여주고 사용자 확인을 받는다 (과금 발생)
        if "받아둔 가격 데이터가 없습니다" in str(e):
            try:
                return {"need_download": True, "cost": D.cost(q.ticker.upper())}
            except Exception as e2:
                return JSONResponse({"error": f"{e}\n비용 조회도 실패: {e2}"}, 400)
        return JSONResponse({"error": str(e)}, 400)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 400)

    try:
        p = Params(**q.model_dump())
        p.ticker = p.ticker.upper()
        res = run(p, rows, dmeta)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 400)

    return {
        "params": res.params, "meta": res.meta, "metrics": res.metrics,
        "cycles": res.cycles, "warnings": res.warnings,
        "trades": res.trades,
        "equity": [[d, v] for d, v in res.equity],
    }


@app.post("/api/download")
def api_download(q: DlReq):
    try:
        D.download(q.ticker.upper())
        return {"ok": True, "cached": D.cached_tickers()}
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 400)


@app.get("/api/cost")
def api_cost(ticker: str):
    try:
        return D.cost(ticker.upper())
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 400)


def main(argv=None):
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)
    print(f"백테스트 UI → http://{a.host}:{a.port}")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
