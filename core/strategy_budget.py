# -*- coding: utf-8 -*-
"""전략별 시드 예산 가드레일.

사용자가 전략별 총 시드(USD)를 명시 할당한다(자동 균등분배 아님).
전략 내 티커별 배분은 각 전략에서 사용자가 수동 입력한 per-ticker 시드의 합.
여기서는 '할당 총액 vs 사용 합계 vs 잔여'를 표시·검증만 한다(주문 로직 무관).

할당 총액은 전략 DB를 건드리지 않기 위해 플랫폼 레벨 JSON에 보관한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from .strategy_adapters import ADAPTERS, DISPLAY_NAMES, active_rows

_BUDGET_FILE = Path(__file__).resolve().parent / "_strategy_budget.json"


def _load() -> dict:
    try:
        return json.loads(_BUDGET_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _BUDGET_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def set_assigned_total(strategy: str, total_usd: float) -> None:
    if strategy not in ADAPTERS:
        raise ValueError(f"알 수 없는 전략: {strategy}")
    if total_usd < 0:
        raise ValueError("할당 총액은 0 이상이어야 합니다.")
    d = _load()
    d[strategy] = float(total_usd)
    _save(d)


def summary() -> list[dict]:
    """전략별 [할당총액, 사용합계(per-ticker seed 합), 잔여, 티커수, 초과여부]."""
    assigned = _load()
    out = []
    for name in ADAPTERS:
        rows = active_rows(name)
        used = round(sum(seed for _, seed in rows), 2)
        total = assigned.get(name)
        remaining = round(total - used, 2) if total is not None else None
        out.append({
            "strategy": name,
            "display_name": DISPLAY_NAMES.get(name, name),
            "assigned_total": total,
            "used": used,
            "remaining": remaining,
            "ticker_count": len(rows),
            "over_budget": (total is not None and used > total),
        })
    return out
