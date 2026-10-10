# -*- coding: utf-8 -*-
"""가격 데이터 — Databento 일봉 다운로드 · 캐시 · 분할조정.

- 티커를 처음 쓰면 상장거래소 데이터셋을 자동으로 찾아 받고 `_cache/` 에 저장한다.
  이후 같은 티커는 네트워크를 타지 않는다(`refresh=True` 로 강제 갱신).
- LOC/MOC 는 상장거래소 종가단일가에서 체결되므로, 통합피드가 아니라
  **그 종목의 상장거래소** 데이터셋을 쓴다. 그래서 종목별로 데이터셋이 다르다.

API 키: 환경변수 DATABENTO_API_KEY, 없으면 backtest/_dbn_key.txt (gitignore 대상).
키를 소스에 적지 말 것.
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import requests

from . import splits as SP

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache"
BASE = "https://hist.databento.com/v0"

# 상장거래소 후보. 앞에서부터 레코드 수를 재서 가장 많은 곳을 그 종목의 상장거래소로 본다.
CANDIDATES = ["XNAS.ITCH", "ARCX.PILLAR", "XNYS.PILLAR", "XASE.PILLAR"]

DATA_START = "2018-05-01"      # Databento 미국주식 커버리지 시작


class DataError(RuntimeError):
    pass


def api_key() -> str:
    k = os.environ.get("DATABENTO_API_KEY", "").strip()
    if k:
        return k
    f = HERE / "_dbn_key.txt"
    if f.exists():
        k = f.read_text(encoding="utf-8").strip()
        if k:
            return k
    raise DataError(
        "Databento API 키가 없습니다. 환경변수 DATABENTO_API_KEY 를 설정하거나 "
        f"{f} 에 키만 한 줄로 저장하세요."
    )


def _get(path: str, params: dict) -> requests.Response:
    r = requests.get(f"{BASE}/{path}", params=params, auth=(api_key(), ""), timeout=120)
    if r.status_code != 200:
        raise DataError(f"Databento {path} 실패 (HTTP {r.status_code}): {r.text[:300]}")
    return r


def available_end() -> str:
    """데이터셋이 보유한 마지막 날짜 (end 가 이보다 뒤면 422 가 난다)."""
    d = _get("metadata.get_dataset_range", {"dataset": "XNAS.ITCH"}).json()
    return str(d.get("end", ""))[:10]


def _symbol_map() -> dict:
    f = CACHE / "_symbols.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_symbol_map(m: dict) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "_symbols.json").write_text(json.dumps(m, ensure_ascii=False, indent=1),
                                         encoding="utf-8")


def resolve_dataset(ticker: str, end: str) -> str:
    """그 티커의 상장거래소 데이터셋. 한 번 찾으면 캐시한다."""
    t = ticker.upper()
    m = _symbol_map()
    if t in m:
        return m[t]
    best, best_n = None, 0
    for ds in CANDIDATES:
        try:
            n = int(_get("metadata.get_record_count", {
                "dataset": ds, "symbols": t, "schema": "ohlcv-1d",
                "start": DATA_START, "end": end, "stype_in": "raw_symbol",
            }).json())
        except Exception:
            n = 0
        if n > best_n:
            best, best_n = ds, n
    if not best:
        raise DataError(f"{t}: Databento 후보 데이터셋 어디에도 일봉이 없습니다. "
                        f"티커를 확인하세요.")
    m[t] = best
    _save_symbol_map(m)
    return best


def cost(ticker: str, end: str | None = None) -> dict:
    """다운로드 전 비용 견적 (실제 과금은 받을 때 발생)."""
    end = end or available_end()
    ds = resolve_dataset(ticker, end)
    p = {"dataset": ds, "symbols": ticker.upper(), "schema": "ohlcv-1d",
         "start": DATA_START, "end": end, "stype_in": "raw_symbol"}
    usd = float(_get("metadata.get_cost", {**p, "mode": "historical-streaming"}).json())
    n = int(_get("metadata.get_record_count", p).json())
    return {"ticker": ticker.upper(), "dataset": ds, "cost_usd": usd, "records": n}


def _cache_file(ticker: str) -> Path:
    return CACHE / f"{ticker.upper()}.csv"


def download(ticker: str, end: str | None = None) -> list[dict]:
    """전체 보유기간 일봉을 받아 캐시에 저장. 반환은 원시가격(미조정)."""
    t = ticker.upper()
    end = end or available_end()
    ds = resolve_dataset(t, end)
    r = _get("timeseries.get_range", {
        "dataset": ds, "symbols": t, "schema": "ohlcv-1d",
        "start": DATA_START, "end": end, "stype_in": "raw_symbol",
        "encoding": "csv", "pretty_px": "true", "pretty_ts": "true",
        "map_symbols": "true",
    })
    rows = []
    for d in csv.DictReader(io.StringIO(r.text)):
        if not d.get("ts_event"):
            continue
        rows.append({
            "date": d["ts_event"][:10],
            "open": float(d["open"]), "high": float(d["high"]),
            "low": float(d["low"]), "close": float(d["close"]),
            "volume": float(d.get("volume") or 0),
        })
    if not rows:
        raise DataError(f"{t}: 받은 데이터가 비어 있습니다 (dataset={ds}).")
    rows.sort(key=lambda x: x["date"])
    CACHE.mkdir(parents=True, exist_ok=True)
    with _cache_file(t).open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(rows)
    return rows


def raw(ticker: str, refresh: bool = False, allow_download: bool = True) -> list[dict]:
    """캐시 우선 원시 일봉.

    allow_download=False 면 캐시에 없을 때 다운로드하지 않고 예외를 던진다.
    (다운로드는 과금되므로 웹 UI 는 사용자가 비용을 확인한 뒤에만 받는다.)
    """
    f = _cache_file(ticker)
    if f.exists() and not refresh:
        with f.open(encoding="utf-8") as fh:
            return [{"date": d["date"], "open": float(d["open"]), "high": float(d["high"]),
                     "low": float(d["low"]), "close": float(d["close"]),
                     "volume": float(d["volume"] or 0)}
                    for d in csv.DictReader(fh)]
    if not allow_download:
        raise DataError(f"{ticker.upper()}: 받아둔 가격 데이터가 없습니다. "
                        f"먼저 다운로드하세요 (과금 발생).")
    return download(ticker)


def load(ticker: str, start: str | None = None, end: str | None = None,
         refresh: bool = False, allow_download: bool = True) -> tuple[list[dict], dict]:
    """분할조정된 일봉 + 메타.

    start/end 로 잘라내되, 분할 누적계수는 **전체 기간**으로 계산한 뒤 자른다
    (구간만 보고 계산하면 구간 밖 분할이 빠져 가격이 틀어진다).
    """
    rows = raw(ticker, refresh=refresh, allow_download=allow_download)
    sp, src = SP.for_ticker(ticker, rows)
    adj = SP.adjust(rows, sp)
    sel = [r for r in adj
           if (not start or r["date"] >= start) and (not end or r["date"] <= end)]
    if not sel:
        raise DataError(f"{ticker.upper()}: {start}~{end} 구간에 데이터가 없습니다 "
                        f"(보유 범위 {adj[0]['date']}~{adj[-1]['date']}).")
    return sel, {
        "ticker": ticker.upper(),
        "splits": [{"date": d, "ratio": r} for d, r in sp],
        "splits_source": src,
        "range_all": [adj[0]["date"], adj[-1]["date"]],
        "range_used": [sel[0]["date"], sel[-1]["date"]],
        "days": len(sel),
        "cached": _cache_file(ticker).exists(),
    }


def cached_tickers() -> list[str]:
    if not CACHE.exists():
        return []
    return sorted(p.stem for p in CACHE.glob("*.csv") if not p.stem.startswith("_"))
