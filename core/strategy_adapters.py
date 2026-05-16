# -*- coding: utf-8 -*-
"""전략별 데이터 어댑터.

각 전략은 DB·스키마가 다르므로(무한매수법=Portfolio.seed, 떨사오팔=Ticker.total_usd),
플랫폼이 '활성 티커'와 '티커별 시드'를 일관 조회할 수 있도록 어댑터로 감싼다.
전략 패키지는 수정하지 않고 읽기만 한다(운영중 코드 무변경 원칙).

신규 전략(종사종팔/무한v3)은 여기 ADAPTERS 항목만 추가하면 된다.
"""
from __future__ import annotations


def _infinite_rows() -> list[tuple[str, float]]:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from strategies.infinite.config import DATABASE_URL
    from strategies.infinite.models import Portfolio
    eng = create_engine(DATABASE_URL)
    with Session(eng) as s:
        return [
            (str(t).upper(), float(seed or 0))
            for t, seed in s.execute(
                select(Portfolio.ticker, Portfolio.seed).where(Portfolio.is_active == True)
            ).all()
        ]


def _ddsop_rows() -> list[tuple[str, float]]:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from strategies.ddsop.config import DATABASE_URL
    from strategies.ddsop.models import Ticker
    eng = create_engine(DATABASE_URL)
    with Session(eng) as s:
        return [
            (str(t).upper(), float(usd or 0))
            for t, usd in s.execute(
                select(Ticker.ticker, Ticker.total_usd).where(Ticker.is_active == True)
            ).all()
        ]


# name -> 활성 (ticker, per_ticker_seed) 목록을 반환하는 콜러블
ADAPTERS = {
    "infinite": _infinite_rows,
    "ddsop": _ddsop_rows,
}

DISPLAY_NAMES = {
    "infinite": "무한매수법 V2.2",
    "ddsop": "떨사오팔",
}


def active_rows(strategy: str) -> list[tuple[str, float]]:
    fn = ADAPTERS.get(strategy)
    if fn is None:
        return []
    try:
        return fn()
    except Exception:
        return []
