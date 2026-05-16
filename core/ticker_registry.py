# -*- coding: utf-8 -*-
"""티커 전역 유일성 레지스트리.

단일 KIS 계좌에서 미체결/체결/잔고/체결통보를 '티커 → 소속 전략'으로
안전하게 귀속시키려면 전략 간 티커가 절대 중복되면 안 된다(P0 불변식).
이 모듈이 그 불변식을 강제한다.
"""
from __future__ import annotations

from .strategy_adapters import ADAPTERS, DISPLAY_NAMES, active_rows


class TickerConflict(ValueError):
    """다른 전략이 이미 보유한 티커를 등록하려 할 때."""


def find_owner(ticker: str, exclude: str | None = None) -> str | None:
    """해당 티커를 현재 보유(active)한 전략명. 없으면 None. exclude 전략은 제외."""
    t = str(ticker or "").strip().upper()
    if not t:
        return None
    for name in ADAPTERS:
        if name == exclude:
            continue
        if t in {tk for tk, _ in active_rows(name)}:
            return name
    return None


def assert_ticker_available(strategy: str, ticker: str) -> None:
    """`strategy`가 `ticker`를 등록 가능한지 검사. 다른 전략 보유 시 TickerConflict.

    같은 전략 내 중복은 각 전략 기존 로직에 위임(여기선 교차 전략만 강제)."""
    owner = find_owner(ticker, exclude=strategy)
    if owner is not None:
        raise TickerConflict(
            f"{str(ticker).strip().upper()} 은(는) 이미 '{DISPLAY_NAMES.get(owner, owner)}' "
            f"전략에서 운용 중입니다. 전략 간 티커는 중복할 수 없습니다."
        )


def all_active() -> dict[str, list[str]]:
    """전략명 → 활성 티커 목록 (대시보드/디버그용)."""
    return {name: sorted(tk for tk, _ in active_rows(name)) for name in ADAPTERS}
