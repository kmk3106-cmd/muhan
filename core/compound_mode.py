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
        "tickers": st.get("tickers") or {},        # 종목별 기준시드·증액 (순차 반영용)
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


def ticker_stats(strategy: str) -> dict:
    """종목별 {seed, realized(완료 싸이클 Σprofit), cycles}.

    증액을 **싸이클이 끝난 종목에만** 적용하려면 종목 단위 실적이 필요하다.
    떨사오팔·종사종팔은 cycle_history.ticker, 무한매수법은 portfolio_id → ticker 로 묶는다.
    """
    out: dict[str, dict] = {}
    try:
        from .strategy_adapters import active_rows
        for tk, seed in active_rows(strategy):
            out[tk] = {"seed": float(seed or 0), "realized": 0.0, "cycles": 0}
    except Exception as e:
        logger.warning(f"[compound] {strategy} 활성종목 조회 실패: {e}")
        return out
    try:
        import importlib
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session
        cfg = importlib.import_module(f"strategies.{strategy}.config")
        models = importlib.import_module(f"strategies.{strategy}.models")
        CH = models.CycleHistory
        eng = create_engine(cfg.DATABASE_URL, connect_args={"timeout": 15})
        with Session(eng) as s:
            if hasattr(CH, "ticker"):                      # 떨사오팔·종사종팔
                rows = s.execute(select(CH.ticker, CH.profit)).all()
            else:                                          # 무한매수법 (portfolio_id → ticker)
                P = models.Portfolio
                rows = s.execute(select(P.ticker, CH.profit)
                                 .join(CH, CH.portfolio_id == P.id)).all()
        for tk, profit in rows:
            k = str(tk or "").upper()
            if k not in out:                                # 비활성 종목의 과거 싸이클은 무시
                continue
            out[k]["realized"] = round(out[k]["realized"] + float(profit or 0), 2)
            out[k]["cycles"] += 1
    except Exception as e:
        logger.warning(f"[compound] {strategy} 종목별 싸이클 조회 실패: {e}")
    return out


def _ticker_state(st: dict, strategy: str) -> dict:
    """복리 상태의 종목별 칸. 없으면 현재 시점 기준으로 초기화(소급 없음)."""
    tk_state = st.get("tickers")
    if isinstance(tk_state, dict) and tk_state:
        return tk_state
    tk_state = {}
    for tk, s in ticker_stats(strategy).items():
        tk_state[tk] = {"base_seed": s["seed"], "baseline_realized": s["realized"],
                        "last_cycles": s["cycles"], "added": 0.0}
    st["tickers"] = tk_state
    return tk_state


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
            # 종목별 기준선 — 증액은 '싸이클이 끝난 종목'에만 순차 반영한다
            "tickers": {tk: {"base_seed": s["seed"], "baseline_realized": s["realized"],
                             "last_cycles": s["cycles"], "added": 0.0}
                        for tk, s in ticker_stats(strategy).items()},
        })
        logger.info(f"[compound] {strategy} 복리 ON (기준시드 ${st['base_seed']:,.0f}, "
                    f"기준실현 ${st['baseline_realized']:,.2f}, 싸이클 {st['last_cycles']}, "
                    f"종목 {list((st.get('tickers') or {}).keys())})")
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
                "raw_target": base, "capped_target": base, "capped": False, "note": "",
                "per_ticker": {}}
    tk_state = st.get("tickers") or {}
    per_ticker = {}
    if tk_state:
        # 종목별: 그 종목이 복리 켠 뒤 낸 실현손익만큼 (싸이클 종료 시 반영 예정)
        stats = ticker_stats(strategy)
        base = round(sum(float(v.get("base_seed") or 0) for v in tk_state.values()), 2)
        gain = 0.0
        for tk, v in tk_state.items():
            s = stats.get(tk) or {"realized": 0.0, "seed": 0.0}
            g = round(max(0.0, s["realized"] - float(v.get("baseline_realized") or 0)), 2)
            per_ticker[tk] = {"base_seed": float(v.get("base_seed") or 0), "gain": g,
                              "added": float(v.get("added") or 0), "seed_now": s.get("seed", 0.0),
                              "next_seed": round(float(v.get("base_seed") or 0) + g, 2)}
            gain = round(gain + g, 2)
    else:
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
            "capped_target": round(base + capped_gain, 2), "capped": capped, "note": note,
            "per_ticker": per_ticker}


def apply_if_cycle_ended(strategy: str) -> dict | None:
    """싸이클이 끝난 **종목만** 시드를 증액. 없으면 None.

    각 전략 워커가 싸이클을 기록한 뒤(= 그 종목 CycleHistory 증가) 호출한다.
    - 증액분 = 복리 켠 뒤 그 종목이 낸 실현손익 (음수면 0 — 손실로 원금을 깎지 않는다)
    - 진행 중인 다른 종목의 시드는 건드리지 않는다 (2026-09-29 수정, 이전에는 균등 재배분했다)
    - 현금 여력 상한은 전략 전체 기준으로 남은 한도를 나눠 쓴다
    """
    st_raw = _load().get(strategy) or {}
    if st_raw.get("mode") != "compound":
        return None
    tk_state = _ticker_state(st_raw, strategy)
    stats = ticker_stats(strategy)
    cash = _account_cash()
    added_all = sum(float(v.get("added") or 0) for v in tk_state.values())
    room = max(0.0, added_all + cash * CASH_BUFFER_RATIO)      # 증액 총량 상한
    changed = []
    for tk, s in stats.items():
        cur = tk_state.get(tk)
        if not cur:                                            # 복리 켠 뒤 추가된 종목 → 지금부터 기준선
            tk_state[tk] = {"base_seed": s["seed"], "baseline_realized": s["realized"],
                            "last_cycles": s["cycles"], "added": 0.0}
            continue
        if s["cycles"] <= int(cur.get("last_cycles") or 0):     # 이 종목은 새 싸이클 종료 없음
            continue
        base = float(cur.get("base_seed") or s["seed"])
        gain = round(max(0.0, s["realized"] - float(cur.get("baseline_realized") or 0)), 2)
        other_added = round(added_all - float(cur.get("added") or 0), 2)
        allow = max(0.0, round(room - other_added, 2))          # 이 종목이 쓸 수 있는 증액 한도
        capped_gain = round(min(gain, allow), 2)
        new_seed = round(base + capped_gain, 2)
        before = s["seed"]
        if abs(new_seed - before) >= 0.01:
            if not _write_seed_one(strategy, tk, new_seed):
                logger.warning(f"[compound] {strategy}:{tk} 시드 반영 실패 — 다음 싸이클에 재시도")
                continue
        cur.update({"last_cycles": s["cycles"], "added": capped_gain,
                    "capped": capped_gain < gain,
                    "cap_note": (f"현금 여력 상한 적용 (증액 {gain:,.0f} → {capped_gain:,.0f}, "
                                 f"예수금 {cash:,.0f})" if capped_gain < gain else "")})
        added_all = round(added_all - float(cur.get("added") or 0) + capped_gain, 2)
        changed.append({"ticker": tk, "cycles": s["cycles"], "seed_before": before,
                        "seed_after": new_seed, "added": capped_gain,
                        "capped": capped_gain < gain})
        logger.info(f"[compound] {strategy}:{tk} 싸이클 종료 감지({cur['last_cycles']}) "
                    f"시드 ${before:,.0f} → ${new_seed:,.0f} (증액 ${capped_gain:,.0f})"
                    + (" · 현금상한 적용" if capped_gain < gain else ""))
    # 상태 저장 (집계값은 화면 표시용)
    d = _load()
    s0 = d.get(strategy) or {}
    s0["tickers"] = tk_state
    s0["added"] = round(sum(float(v.get("added") or 0) for v in tk_state.values()), 2)
    s0["base_seed"] = round(sum(float(v.get("base_seed") or 0) for v in tk_state.values()), 2)
    s0["last_cycles"] = _cur_cycles(strategy)
    s0["capped"] = any(v.get("capped") for v in tk_state.values())
    s0["cap_note"] = next((v.get("cap_note") for v in tk_state.values() if v.get("cap_note")), "")
    d[strategy] = s0
    _save(d)
    if not changed:
        return None
    return {"strategy": strategy, "changed": changed,
            "added_total": s0["added"], "capped": s0["capped"], "note": s0["cap_note"]}


def _write_seed_one(strategy: str, ticker: str, seed: float) -> bool:
    """그 종목 하나의 시드만 기록 (다른 종목은 건드리지 않는다)."""
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
                row = s.scalar(select(P).where(P.is_active == True, P.ticker == ticker))  # noqa: E712
                if not row:
                    return False
                row.seed = seed
            else:                                   # 떨사오팔 / 종사종팔
                Tk = models.Ticker
                row = s.scalar(select(Tk).where(Tk.is_active == True, Tk.ticker == ticker))  # noqa: E712
                if not row:
                    return False
                row.total_usd = seed
            s.commit()
        return True
    except Exception as e:
        logger.warning(f"[compound] _write_seed_one({strategy}:{ticker}) 실패: {e}")
        return False


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
    d = _load(); s = d.get(strategy) or {}
    tk_state = s.get("tickers") or {}
    # 복리 켤 때 기록해 둔 **종목별 기준시드**로 되돌린다 (균등 재배분 금지)
    if tk_state:
        restored = {}
        for tk, v in tk_state.items():
            base = float(v.get("base_seed") or 0)
            if base > 0 and _write_seed_one(strategy, tk, base):
                restored[tk] = base
            v["added"] = 0.0
        s.update({"mode": "simple", "added": 0.0, "capped": False, "cap_note": "", "tickers": tk_state})
        d[strategy] = s; _save(d)
        logger.info(f"[compound] {strategy} 단리 복원: 종목별 기준시드 {restored}")
        return {"restored": bool(restored), "seeds": restored}
    st = get(strategy)
    target = assigned if assigned else st.get("base_seed")
    if not target:
        return {"restored": False, "reason": "시드 할당 총액 미설정"}
    ok = _write_seed(strategy, float(target))
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
