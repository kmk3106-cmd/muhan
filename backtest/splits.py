# -*- coding: utf-8 -*-
"""분할·병합 처리.

Databento 는 체결 당시 원시가격(as-traded)을 준다. 분할을 반영하지 않으면 분할일에
가격이 반토막 나면서 '거짓 폭락 → 거짓 추가매수 → 거짓 손실'이 연쇄로 생긴다.

두 가지 경로를 쓴다.
  1) VERIFIED — 웹 조사로 확정하고 데이터로 교차검증까지 끝낸 종목 (출처는 README).
  2) autodetect() — 미등록 티커는 데이터에서 직접 역추적한다.

역추적 원리
  분할일 D 에는 가격이 ratio 배로 쪼개지므로  close[D-1] / open[D] ≈ ratio.
  실제 폭락은 open[D] 가 close[D-1] 근처에서 열리고 장중에 빠지므로 이 비율이 ≈1 이다.
  이 차이가 분할과 폭락을 가른다 (2020-03 코로나 폭락 오탐 0건 확인).
"""
from __future__ import annotations

# 교차검증 완료 (웹 공시비율 == 데이터 측정비율, 2018-05-01 이후 구간)
VERIFIED: dict[str, list[tuple[str, float]]] = {
    "TQQQ": [("2018-05-24", 3), ("2021-01-21", 2), ("2022-01-13", 2), ("2025-11-20", 2)],
    "QLD":  [("2020-08-18", 2), ("2021-05-25", 2), ("2025-11-20", 2)],
    "SOXL": [("2021-03-02", 15)],
    "TECL": [("2021-03-02", 10)],
    "UPRO": [("2018-05-24", 3), ("2022-01-13", 2)],
    "SPXL": [],
}

# 역추적 비율을 여기로 스냅한다. 분할은 거의 항상 이 중 하나다.
COMMON = [2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30]


def _snap(r: float) -> float | None:
    """측정비율을 가까운 통상 비율로. 분할(>1)과 병합(<1) 모두."""
    if r >= 1.6:
        best = min(COMMON, key=lambda c: abs(c - r))
        return float(best) if abs(best - r) / best < 0.12 else None
    if r <= 0.625:                      # 병합: 1:n → 비율 1/n
        inv = 1.0 / r
        best = min(COMMON, key=lambda c: abs(c - inv))
        return 1.0 / best if abs(best - inv) / best < 0.12 else None
    return None


def autodetect(rows: list[dict]) -> list[tuple[str, float, float]]:
    """일봉(date·open·close 정렬됨)에서 분할 역추적.

    반환: [(date, 스냅된 비율, 측정 원값)]
    """
    out = []
    for i in range(1, len(rows)):
        cp = float(rows[i - 1].get("close") or 0)
        op = float(rows[i].get("open") or 0)
        if cp <= 0 or op <= 0:
            continue
        raw = cp / op
        snapped = _snap(raw)
        if snapped:
            out.append((rows[i]["date"], snapped, round(raw, 4)))
    return out


def for_ticker(ticker: str, rows: list[dict]) -> tuple[list[tuple[str, float]], str]:
    """해당 티커에 쓸 분할 목록과 출처 표시.

    VERIFIED 에 있으면 그걸 쓰고(공시비율이 더 정확하다), 없으면 역추적한다.
    """
    t = ticker.upper()
    if t in VERIFIED:
        return list(VERIFIED[t]), "verified"
    det = autodetect(rows)
    return [(d, r) for d, r, _ in det], "autodetect"


def adjust(rows: list[dict], splits: list[tuple[str, float]]) -> list[dict]:
    """분할 역조정 — 오늘 주식 수 기준으로 과거 가격을 연속화한다.

    누적계수(factor) = 그 날짜 '이후'에 일어난 모든 분할 비율의 곱.
    조정가 = 원시가 / factor, 조정거래량 = 원시거래량 × factor.
    """
    out = []
    for r in rows:
        f = 1.0
        for d, ratio in splits:
            if r["date"] < d:
                f *= ratio
        x = dict(r)
        for c in ("open", "high", "low", "close"):
            if x.get(c):
                x[c] = float(x[c]) / f
        if x.get("volume"):
            x["volume"] = float(x["volume"]) * f
        x["adj_factor"] = f
        out.append(x)
    return out
