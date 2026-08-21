# -*- coding: utf-8 -*-
"""복리 모드(원금증액) 관리 — 무한매수법·떨사오팔·종사종팔.

확정 규칙 (사용자, 2026-08):
- 단리(기본): 시드 고정. 현행 동작 그대로.
- 복리: **싸이클 종료 시점에만** 시드를 증액하고 **다음 싸이클부터** 적용.
    증액시드 = 최초시드 + (복리 켠 이후 누적 실현손익)          ← 소급 없음
    상한     = 계좌 현금 여력 기반 (B안) — 초과분은 캡, 사유 기록
- 손실이어도 **감액하지 않는다**(하한 = 최초시드).
- 손절(MOC)은 싸이클 종료가 아니므로 증액 트리거가 아니다(각 전략 워커의
  싸이클 기록 로직이 이미 이를 구분 — 여기서는 '기록된 싸이클' 증가만 본다).
- 복리 → 단리 전환 시 시드는 **전략관리의 시드 할당 총액** 기준으로 복원.

저장: core/_compound.json (배포 보존, gitignore)
    { "<strategy>": {
        "mode": "simple"|"compound",
        "enabled_at": "2026-08-21T22:00:00",
        "base_seed": 10000.0,          # 켠 시점 시드 합(복원 기준)
        "baseline_realized": 4329.10,  # 켠 시점 누적 실현손익(이후분만 가산)
        "added": 0.0,                  # 현재까지 반영된 증액분
        "last_cycles": 12,             # 마지막으로 본 완료 싸이클 수(종료 감지용)
        "capped": false, "cap_note": "" } }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("trading_suite.compound")

_FILE = Path(__file__).resolve().parent / "_compound.json"

CASH_BUFFER_RATIO = 0.90   # 계좌 현금 여력의 90%까지만 증액 허용(주문 여유분 유지)


def _load() -> dict:
    try:
        d = json.loads(_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        _FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[compound] 저장 실패: {e}")


def get(strategy: str) -> dict:
    st = _load().get(strategy) or {}
    return {
        "mode": st.get("mode", "simple"),
        "enabled_at": st.get("enabled_at"),
        "base_seed": st.get("base_seed"),
        "baseline_realized": st.get("baseline_realized"),
        "added": float(st.get("added") or 0),
        "last_cycles": st.get("last_cycles"),
        "capped": bool(st.get("capped")),
        "cap_note": st.get("cap_note", ""),
    }


def all_states() -> dict:
    return {k: get(k) for k in ("infinite", "ddsop", "jongsa")}


def _cur_seed_total(strategy: str) -> float:
    """전략의 현재 시드 합(종목별 시드 합계)."""
    try:
        from .strategy_adapters import active_rows
        return round(sum(s for _, s in active_rows(strategy)), 2)
    except Exception:
        return 0.0


def _cur_realized(strategy: str) -> float:
    """전략 누적 실현손익(완료 싸이클 Σprofit)."""
    try:
        from .suite_metrics import _cycles
        cy = _cycles(strategy)
        return float(cy.get("realized") or 0)
    except Exception:
        return 0.0


def _cur_cycles(strategy: str) -> int:
    try:
        from .suite_metrics import _cycles
        return int(_cycles(strategy).get("cycles") or 0)
    except Exception:
        return 0


def set_mode(strategy: str, mode: str) -> dict:
    """단리/복리 전환. 복리 켤 때 기준선(baseline) 고정 — 과거 실현손익은 소급 안 함."""
    if mode not in ("simple", "compound"):
        raise ValueError("mode는 simple 또는 compound")
    d = _load()
    st = d.get(strategy) or {}
    if mode == "compound":
        st.update({
            "mode": "compound",
            "enabled_at": datetime.now().isoformat(timespec="seconds"),
            "base_seed": _cur_seed_total(strategy),        # 복원 기준(전략관리 할당 총액)
            "baseline_realized": _cur_realized(strategy),  # 이 시점 이후 실현분만 가산
            "added": 0.0,
            "last_cycles": _cur_cycles(strategy),
            "capped": False, "cap_note": "",
        })
        logger.info(f"[compound] {strategy} 복리 ON (기준시드 ${st['base_seed']:,.0f}, "
                    f"기준실현 ${st['baseline_realized']:,.2f}, 싸이클 {st['last_cycles']})")
    else:
        st.update({"mode": "simple"})
        logger.info(f"[compound] {strategy} 단리 전환 — 시드는 할당 총액 기준으로 복원 필요")
    d[strategy] = st
    _save(d)
    return get(strategy)


def _account_cash() -> float:
    """공용계좌 예수금(USD). 캐시된 계좌요약 사용 — KIS 무호출."""
    try:
        from .suite_metrics import _account
        best, ts = {}, ""
        for k in ("infinite", "ddsop", "jongsa"):
            a = _account(k) or {}
            t = str(a.get("updated_at") or "")
            if a and (not best or t > ts):
                best, ts = a, t
        return float(best.get("cash") or 0)
    except Exception:
        return 0.0


def target_seed(strategy: str) -> dict:
    """복리 목표 시드 계산 (증액 적용 전 계산기).

    반환: {mode, base_seed, gain, raw_target, capped_target, capped, note}
    """
    st = get(strategy)
    base = float(st.get("base_seed") or _cur_seed_total(strategy))
    if st["mode"] != "compound":
        return {"mode": "simple", "base_seed": base, "gain": 0.0,
                "raw_target": base, "capped_target": base, "capped": False, "note": ""}
    gain = round(max(0.0, _cur_realized(strategy) - float(st.get("baseline_realized") or 0)), 2)
    raw = round(base + gain, 2)
    # 현금 여력 상한: 이미 반영된 증액분(added)은 현금에서 빠져나간 게 아니므로 함께 고려
    cash = _account_cash()
    allow = round(float(st.get("added") or 0) + cash * CASH_BUFFER_RATIO, 2)
    capped_gain = min(gain, max(0.0, allow))
    capped = capped_gain < gain
    note = (f"현금 여력 상한 적용 (증액 {gain:,.0f} → {capped_gain:,.0f}, 예수금 {cash:,.0f})"
            if capped else "")
    return {"mode": "compound", "base_seed": base, "gain": gain, "raw_target": raw,
            "capped_target": round(base + capped_gain, 2), "capped": capped, "note": note}


def apply_if_cycle_ended(strategy: str) -> dict | None:
    """싸이클이 새로 종료됐으면 시드를 증액 적용. 아니면 None.

    각 전략 워커가 싸이클을 기록한 뒤(= CycleHistory 증가) 호출된다.
    손절(MOC)은 워커가 싸이클로 기록하지 않으므로 여기 트리거되지 않는다.
    """
    st = get(strategy)
    if st["mode"] != "compound":
        return None
    now_cycles = _cur_cycles(strategy)
    last = st.get("last_cycles")
    if last is not None and now_cycles <= int(last):
        return None                      # 새 싸이클 종료 없음
    tgt = target_seed(strategy)
    new_total = tgt["capped_target"]
    cur_total = _cur_seed_total(strategy)
    added = round(new_total - float(tgt["base_seed"]), 2)
    changed = abs(new_total - cur_total) >= 0.01
    if changed:
        ok = _write_seed(strategy, new_total)
        if not ok:
            logger.warning(f"[compound] {strategy} 시드 반영 실패 — 다음 싸이클에 재시도")
            return None
    d = _load()
    s = d.get(strategy) or {}
    s.update({"last_cycles": now_cycles, "added": added,
              "capped": tgt["capped"], "cap_note": tgt["note"]})
    d[strategy] = s
    _save(d)
    logger.info(f"[compound] {strategy} 싸이클 종료 감지({last}→{now_cycles}) "
                f"시드 ${cur_total:,.0f} → ${new_total:,.0f} (증액 ${added:,.0f})"
                + (f" · {tgt['note']}" if tgt["capped"] else ""))
    return {"strategy": strategy, "cycles": now_cycles,
            "seed_before": cur_total, "seed_after": new_total, "added": added,
            "capped": tgt["capped"], "note": tgt["note"]}


def restore_simple(strategy: str) -> dict:
    """단리 복원 — 전략관리의 '시드 할당 총액' 기준으로 시드 되돌림."""
    from .strategy_budget import summary
    assigned = None
    try:
        for b in summary():
            if b.get("strategy") == strategy:
                assigned = b.get("assigned_total")
                break
    except Exception:
        pass
    st = get(strategy)
    target = assigned if assigned else st.get("base_seed")
    if not target:
        return {"restored": False, "reason": "시드 할당 총액 미설정"}
    ok = _write_seed(strategy, float(target))
    d = _load(); s = d.get(strategy) or {}
    s.update({"mode": "simple", "added": 0.0, "capped": False, "cap_note": ""})
    d[strategy] = s; _save(d)
    logger.info(f"[compound] {strategy} 단리 복원: 시드 → ${float(target):,.0f} (할당 총액 기준)")
    return {"restored": bool(ok), "seed": float(target)}


def _write_seed(strategy: str, total_usd: float) -> bool:
    """전략 시드를 활성 종목에 균등 배분해 기록 (다음 싸이클부터 유효).

    - infinite : Portfolio.seed        (B = seed/A 로 자동 반영)
    - ddsop/jongsa: Ticker.total_usd   (트렌치 금액 = total_usd/num_tranches)
    """
    import importlib
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session
        cfg = importlib.import_module(f"strategies.{strategy}.config")
        models = importlib.import_module(f"strategies.{strategy}.models")
        eng = create_engine(cfg.DATABASE_URL, connect_args={"timeout": 15})
        with Session(eng) as s:
            if hasattr(models, "Portfolio"):        # 무한매수법
                P = models.Portfolio
                rows = s.scalars(select(P).where(P.is_active == True)).all()  # noqa: E712
                if not rows:
                    return False
                each = round(total_usd / len(rows), 2)
                for p in rows:
                    p.seed = each
            else:                                   # 떨사오팔 / 종사종팔
                Tk = models.Ticker
                rows = s.scalars(select(Tk).where(Tk.is_active == True)).all()  # noqa: E712
                if not rows:
                    return False
                each = round(total_usd / len(rows), 2)
                for t in rows:
                    t.total_usd = each
            s.commit()
        return True
    except Exception as e:
        logger.warning(f"[compound] _write_seed({strategy}) 실패: {e}")
        return False
