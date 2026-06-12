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


def _holdings_for(strategy: str) -> list[dict]:
    """해당 전략 소속(티커 귀속) 현재 보유종목 — 보유수량 캐시 기반(KIS 무호출)."""
    try:
        from .holdings_cache import load
        from .ticker_registry import find_owner
        d = load()
        return [it for it in d.get("items", []) if find_owner(it.get("ticker", "")) == strategy]
    except Exception:
        return []


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

    # 현재 보유 현황(지금까지 누적 보유수량 + 평단·현재가·평가수익률)
    hold = _holdings_for(strategy)
    if hold:
        hqty = sum(int(h.get("qty", 0) or 0) for h in hold)
        heval = round(sum(float(h.get("eval_amt", 0) or 0) for h in hold), 2)
        hbuy = round(sum(float(h.get("buy_amt", 0) or 0) for h in hold), 2)
        hpnl = round(heval - hbuy, 2)
        hrt = round(hpnl / hbuy * 100, 2) if hbuy > 0 else 0.0
        sg = "+" if hpnl >= 0 else ""
        L.append("")
        L.append(f"▪ 보유 현황 (누적 {hqty}주 · 평가 {_money(heval)} · 평가손익 {sg}{_money(hpnl)} {sg}{hrt:.2f}%)")
        for h in hold:
            rt = float(h.get("pnl_rt", 0) or 0)
            rsg = "+" if rt >= 0 else ""
            L.append(f"   · {h['ticker']} {h['qty']}주 · 평단 {_money(h.get('avg_price'))} · "
                     f"현재 {_money(h.get('now_price'))} ({rsg}{rt:.2f}%)")

    L.append("")
    L.append(f"#자동매매 #{name.replace(' ', '')} #미국주식 #{d}")

    return {
        "strategy": strategy, "display_name": name, "date": ymd,
        "buy_count": len(buys), "buy_sum": buy_sum,
        "sell_count": len(sells), "sell_sum": sell_sum,
        "cycles_ended": ended, "has_activity": bool(buys or sells or ended),
        "holdings": hold, "holdings_qty": sum(int(h.get("qty", 0) or 0) for h in hold),
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


def _intro_params(strategy: str) -> dict:
    """전략 소개용 대표 파라미터(활성 종목 1개 기준). 실패 시 빈 dict."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    models = _imp(strategy, "models")
    p: dict = {}
    try:
        with Session(_engine(strategy)) as s:
            if hasattr(models, "Portfolio"):  # 무한매수법 계열
                P = models.Portfolio
                row = s.scalars(select(P).where(P.is_active == True)).first()  # noqa: E712
                if row is not None:
                    p = {"A": int(getattr(row, "A", 40) or 40),
                         "R": float(getattr(row, "R", 10) or 10)}
            elif hasattr(models, "Ticker"):  # 떨사오팔/종사종팔 계열
                Tk = models.Ticker
                row = s.scalars(select(Tk).where(Tk.is_active == True)).first()  # noqa: E712
                if row is not None:
                    p = {"x_pct": float(getattr(row, "x_pct", 0) or 0),
                         "num_tranches": int(getattr(row, "num_tranches", 0) or 0),
                         "loss_cut_days": int(getattr(row, "loss_cut_days", 40) or 40)}
    except Exception:
        p = {}
    return p


def _build_intro(strategy: str, name: str, tickers: list[str], p: dict) -> str:
    tk = ", ".join(tickers) if tickers else "(미등록)"
    H = "\n"
    if strategy == "infinite":
        A = p.get("A", 40)
        R = p.get("R", 10)
        return H.join([
            f"📌 [{name}] 전략 소개",
            "",
            f"무한매수법은 시드를 {A}회로 나눠 매일 분할 매수하고, 평단 대비 목표수익률에 도달하면 전량 매도해 한 싸이클을 마치는 방법입니다.",
            "",
            f"▪ 매수: 매 거래일 LOC 분할매수 ({A}분할). 전반전(T<20)·후반전(T≥20), {A}회차 도달 시 QUARTER(쿼터손절) 모드",
            f"▪ 매도: 평단가 +{R:.0f}% 도달 시 LOC 전량매도 → 싸이클 종료 후 재시작",
            "▪ 핵심지표 T(회차) = 보유 매입금액 ÷ 1회매수금액 (현재 노출도)",
            f"▪ 운용 종목: {tk}",
            "",
            f"#무한매수법 #자동매매 #미국주식 #레버리지ETF",
        ])
    if strategy == "ddsop":
        x = p.get("x_pct", 0)
        n = p.get("num_tranches", 0)
        lc = p.get("loss_cut_days", 40)
        return H.join([
            f"📌 [{name}] 전략 소개",
            "",
            f"\"떨어지면 사고 오르면 판다.\" 총액을 {n or 'n'}개 트렌치로 나눠, 전일 종가보다 떨어지면 매수하고 평단 대비 오르면 매도합니다.",
            "",
            f"▪ 매수: 전일 종가 −{x:g}% 가격에 LOC 매수 (하락 시 체결, 하루 1트렌치)",
            f"▪ 매도: 각 트렌치 평단 +{x:g}% 도달 시 LOC 익절",
            f"▪ 손절: {lc}거래일 경과 트렌치는 MOC 손절",
            "▪ 싸이클: 1번 트렌치 매도로 종료 → 재시작",
            f"▪ 운용 종목: {tk}",
            "",
            "#떨사오팔 #자동매매 #미국주식 #분할매수",
        ])
    if strategy == "jongsa":
        x = p.get("x_pct", 3.5)
        n = p.get("num_tranches", 7)
        lc = p.get("loss_cut_days", 40)
        return H.join([
            f"📌 [{name}] 전략 소개",
            "",
            f"\"종가에 사고 종가에 판다.\" 매 거래일 종가에 다음 트렌치를 매수해 모으고, 보유 전체의 평균단가 대비 목표수익률에 도달하면 전량을 한 번에 매도합니다.",
            "",
            f"▪ 매수: 매 거래일 다음 트렌치 1칸을 종가에 매수 (LOC, 전일종가 기준 한도 · 동시 보유 최대 {n or 7}칸)",
            f"▪ 매도: 보유 전체 평단(가중평균) +{x:g}%(목표수익률) 도달 시 전량 LOC 일괄매도",
            f"▪ 손절: {lc}거래일 경과 트렌치는 MOC 손절",
            "▪ 싸이클: 전량 매도로 종료 → 재시작",
            f"▪ 운용 종목: {tk}",
            "",
            "#종사종팔 #자동매매 #미국주식 #종가매매",
        ])
    return H.join([f"📌 [{name}] 전략 소개", "", f"▪ 운용 종목: {tk}", "", "#자동매매 #미국주식"])


def strategy_intros() -> list[dict]:
    """전략별 소개(첫 글용) 텍스트 — 현재 설정(종목·목표%·트렌치) 반영."""
    from .strategy_adapters import ADAPTERS, DISPLAY_NAMES, active_rows
    out = []
    for k in ADAPTERS:
        name = DISPLAY_NAMES.get(k, k)
        try:
            tickers = [t for t, _ in active_rows(k)]
        except Exception:
            tickers = []
        try:
            p = _intro_params(k)
        except Exception:
            p = {}
        out.append({"strategy": k, "display_name": name, "intro": _build_intro(k, name, tickers, p)})
    return out


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
