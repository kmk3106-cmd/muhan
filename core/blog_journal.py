# -*- coding: utf-8 -*-
"""전략별 일자별 매매일지 — 네이버 블로그 복붙용 텍스트 생성.

KIS 무호출·DB만. 각 전략의 그날 매수/매도/싸이클종료를 모아 전략별 1블록 텍스트로 생성.
무한매수법(Trade.portfolio_id·buy_seq) / 떨사오팔·종사종팔(Trade.ticker·tranche_num·cycle_number)
스키마 차이를 흡수한다.
"""
from __future__ import annotations

import importlib
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _KST = ZoneInfo("Asia/Seoul")
except Exception:  # pragma: no cover
    _KST = None


def _today_ymd() -> str:
    return (datetime.now(_KST) if _KST else datetime.now()).strftime("%Y%m%d")


def _imp(strategy: str, mod: str):
    return importlib.import_module(f"strategies.{strategy}.{mod}")


def _engine(strategy: str):
    from sqlalchemy import create_engine
    return create_engine(_imp(strategy, "config").DATABASE_URL)


def _money(v) -> str:
    try:
        return "${:,.2f}".format(float(v))
    except Exception:
        return "$0.00"


def _trades_on(strategy: str, ymd: str) -> list[dict]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    models = _imp(strategy, "models")
    T = models.Trade
    out: list[dict] = []
    with Session(_engine(strategy)) as s:
        rows = s.scalars(select(T).where(T.trade_date == ymd).order_by(T.id)).all()
        tmap: dict = {}
        if rows and not getattr(rows[0], "ticker", None) and hasattr(rows[0], "portfolio_id"):
            P = getattr(models, "Portfolio", None)
            if P is not None:
                pids = {getattr(r, "portfolio_id", None) for r in rows
                        if getattr(r, "portfolio_id", None) is not None}
                if pids:
                    for pid, tkr in s.execute(select(P.id, P.ticker).where(P.id.in_(pids))).all():
                        tmap[pid] = tkr
        for t in rows:
            tkr = getattr(t, "ticker", "") or tmap.get(getattr(t, "portfolio_id", None), "?")
            seq = getattr(t, "tranche_num", None)
            if seq is None:
                bs = getattr(t, "buy_seq", None)
                try:
                    seq = int(bs) if bs not in (None, "") else None
                except Exception:
                    seq = None
            price = float(getattr(t, "price", 0) or 0)
            qty = int(getattr(t, "qty", 0) or 0)
            out.append({
                "ticker": tkr, "side": getattr(t, "side", ""),
                "order_type": str(getattr(t, "order_type", "") or ""),
                "price": price, "qty": qty, "amount": round(price * qty, 2), "seq": seq,
            })
    return out


def _cycles_ended_on(strategy: str, ymd: str) -> list[dict]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    models = _imp(strategy, "models")
    CH = models.CycleHistory
    out: list[dict] = []
    with Session(_engine(strategy)) as s:
        rows = s.scalars(select(CH).where(CH.end_date == ymd)).all()
        tmap: dict = {}
        if rows and not getattr(rows[0], "ticker", None) and hasattr(rows[0], "portfolio_id"):
            P = getattr(models, "Portfolio", None)
            if P is not None:
                for pid, tkr in s.execute(select(P.id, P.ticker)).all():
                    tmap[pid] = tkr
        for c in rows:
            tkr = getattr(c, "ticker", "") or tmap.get(getattr(c, "portfolio_id", None), "?")
            out.append({
                "ticker": tkr, "cycle": int(getattr(c, "cycle_number", 0) or 0),
                "profit": float(getattr(c, "profit", 0) or 0),
                "profit_pct": float(getattr(c, "profit_pct", 0) or 0),
                "start_date": str(getattr(c, "start_date", "") or ""),
            })
    return out


def _side_label(order_type: str, side: str) -> str:
    if side == "sell":
        return "손절매도(MOC)" if str(order_type).upper() == "MOC" else "익절매도(LOC)"
    return "매수"


def _strategy_block(strategy: str, name: str, ymd: str) -> dict:
    trades = _trades_on(strategy, ymd)
    buys = [t for t in trades if t["side"] == "buy"]
    sells = [t for t in trades if t["side"] == "sell"]
    ended = _cycles_ended_on(strategy, ymd)
    buy_sum = round(sum(t["amount"] for t in buys), 2)
    sell_sum = round(sum(t["amount"] for t in sells), 2)
    d = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"

    L = [f"📈 [{name}] {d} 매매일지", ""]
    if buys:
        L.append(f"▪ 매수 {len(buys)}건 · 합계 {_money(buy_sum)}")
        for t in buys:
            seq = f" {t['seq']}회차" if t["seq"] else ""
            L.append(f"   · {t['ticker']}{seq} {t['qty']}주 @ {_money(t['price'])} = {_money(t['amount'])}")
    else:
        L.append("▪ 매수 없음")
    if sells:
        L.append(f"▪ 매도 {len(sells)}건 · 합계 {_money(sell_sum)}")
        for t in sells:
            seq = f" {t['seq']}회차" if t["seq"] else ""
            L.append(f"   · {t['ticker']}{seq} {_side_label(t['order_type'], 'sell')} "
                     f"{t['qty']}주 @ {_money(t['price'])} = {_money(t['amount'])}")
    else:
        L.append("▪ 매도 없음")
    for c in ended:
        sign = "+" if c["profit"] >= 0 else ""
        L.append(f"▪ 🎯 싸이클 종료: {c['ticker']} C{c['cycle']} "
                 f"손익 {sign}{_money(c['profit'])} ({sign}{c['profit_pct']:.2f}%)")
    if not buys and not sells and not ended:
        L.append("(당일 체결·싸이클 변동 없음)")
    L.append("")
    L.append(f"#자동매매 #{name.replace(' ', '')} #미국주식 #{d}")

    return {
        "strategy": strategy, "display_name": name, "date": ymd,
        "buy_count": len(buys), "buy_sum": buy_sum,
        "sell_count": len(sells), "sell_sum": sell_sum,
        "cycles_ended": ended, "has_activity": bool(buys or sells or ended),
        "text": "\n".join(L),
    }


def daily(ymd: str | None = None) -> dict:
    from .strategy_adapters import ADAPTERS, DISPLAY_NAMES
    ymd = (ymd or _today_ymd()).replace("-", "")[:8]
    blocks = []
    for k in ADAPTERS:
        try:
            blocks.append(_strategy_block(k, DISPLAY_NAMES.get(k, k), ymd))
        except Exception as e:
            blocks.append({"strategy": k, "display_name": DISPLAY_NAMES.get(k, k),
                           "date": ymd, "error": f"{type(e).__name__}: {e}", "text": ""})
    return {"date": ymd, "strategies": blocks}


def latest_active_date() -> str:
    """전 전략 통틀어 체결이 있었던 최근 일자 (없으면 오늘)."""
    from .strategy_adapters import ADAPTERS
    from sqlalchemy import select, func
    from sqlalchemy.orm import Session
    best = ""
    for k in ADAPTERS:
        try:
            T = _imp(k, "models").Trade
            with Session(_engine(k)) as s:
                m = s.scalar(select(func.max(T.trade_date)))
            if m and str(m) > best:
                best = str(m)
        except Exception:
            pass
    return best or _today_ymd()
