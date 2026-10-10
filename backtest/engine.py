# -*- coding: utf-8 -*-
"""백테스트 엔진 — 무한매수법 V2.2 · 떨사오팔 · 종사종팔4.

원칙: **매매 규칙을 다시 쓰지 않는다.**
각 전략 패키지의 운영 `trading_logic.generate_orders()` 를 그대로 import 해서 호출한다.
DB 모델 대신 같은 필드를 가진 가벼운 객체를 넘긴다(읽기만 하므로 안전).
체결·상태갱신 규칙은 각 전략의 운영 `simulate.py` 와 동일하게 맞췄다.

따라서 운영 코드의 규칙이 바뀌면 이 백테스트 결과도 자동으로 따라간다.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STRATEGIES = {
    "infinite": "무한매수법 V2.2",
    "ddsop": "떨사오팔",
    "jongsa": "종사종팔4",
}

# 운영 중인 설정 (서버 DB is_active=1 기준, 프리셋 기본값으로 쓴다)
PRESETS = {
    "infinite": {"SOXL": {"seed": 5000, "A": 40, "R": 12.0},
                 "TQQQ": {"seed": 5000, "A": 40, "R": 10.0}},
    "ddsop": {"TECL": {"seed": 12524.45, "num_tranches": 7, "x_pct": 0.5, "loss_cut_days": 40},
              "SPXL": {"seed": 12500.0, "num_tranches": 7, "x_pct": 0.5, "loss_cut_days": 40}},
    "jongsa": {"UPRO": {"seed": 15000.0, "num_tranches": 7, "x_pct": 2.7, "loss_cut_days": 7}},
}


@dataclass
class Params:
    strategy: str = "infinite"
    ticker: str = "TQQQ"
    start: str = "2018-05-01"
    end: str = ""
    seed: float = 5000.0
    fee_pct: float = 0.0              # 편도 수수료 % (0.25 = 0.25%)
    # 무한매수법
    A: int = 40
    R: float = 10.0
    compound: bool = False            # 운영 복리모드(싸이클 종료 시 증액시드)
    # 떨사오팔 / 종사종팔
    num_tranches: int = 7
    x_pct: float = 2.7
    loss_cut_days: int = 40
    seed_reflect: bool = False
    v4_compound: bool = False         # 종사종팔4 익절복리(익절 ÷ 트렌치수 누적)

    @property
    def fee(self) -> float:
        return float(self.fee_pct) / 100.0


@dataclass
class Result:
    params: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    cycles: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


# ══════════════════ 공통 ══════════════════
def _mdd(eq: list) -> float:
    if not eq:
        return 0.0
    peak, worst = eq[0][1], 0.0
    for _, v in eq:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak * 100)
    return round(worst, 2)


def _buyhold(rows: list[dict], seed: float) -> tuple[list, float]:
    p0 = rows[0]["close"]
    if p0 <= 0:
        return [], seed
    sh = seed / p0
    eq = [(r["date"], r["close"] * sh) for r in rows]
    return eq, eq[-1][1]


def _metrics(res: Result, rows: list[dict], seed: float) -> None:
    eq = res.equity
    final = eq[-1][1] if eq else seed
    yrs = max(len(rows) / 252.0, 1e-9)
    wins = sum(1 for c in res.cycles if c["profit"] > 0)
    realized = sum(c["profit"] for c in res.cycles)
    bh_eq, bh_final = _buyhold(rows, seed)
    m = {
        "final_equity": round(final, 2),
        "total_return_pct": round((final / seed - 1) * 100, 2),
        "cagr_pct": round(((final / seed) ** (1 / yrs) - 1) * 100, 2),
        "mdd_pct": _mdd(eq),
        "realized": round(realized, 2),
        "cycles": len(res.cycles),
        "wins": wins,
        "losses": len(res.cycles) - wins,
        "win_rate": round(wins / len(res.cycles) * 100, 1) if res.cycles else None,
        "avg_cycle_days": (round(sum(c["days"] for c in res.cycles) / len(res.cycles), 1)
                           if res.cycles else None),
        "trades": len(res.trades),
        "fees_paid": round(sum(t.get("fee", 0) for t in res.trades), 2),
        "years": round(yrs, 2),
        "bh_final": round(bh_final, 2),
        "bh_return_pct": round((bh_final / seed - 1) * 100, 2),
        "bh_cagr_pct": round(((bh_final / seed) ** (1 / yrs) - 1) * 100, 2),
        "bh_mdd_pct": _mdd(bh_eq),
    }
    for k, pre in (("", ""), ("bh_", "bh_")):
        cg, md = m[f"{pre}cagr_pct"], m[f"{pre}mdd_pct"]
        m[f"{pre}rr"] = round(cg / abs(md), 3) if md else None
    res.metrics = m
    res.meta["buyhold_equity"] = [(d, round(v, 2)) for d, v in bh_eq]


# ══════════════════ 무한매수법 V2.2 ══════════════════
class _PF:
    def __init__(self, p: Params):
        self.ticker, self.seed, self.A, self.R = p.ticker, p.seed, p.A, p.R
        self.strategy_version = "2.2"

    @property
    def B(self) -> float:
        return self.seed / self.A if self.A else 0.0


@dataclass
class _ST:
    avg_price: float = 0.0
    qty: int = 0
    T: float = 0.0
    star_pct: float = 10.0
    mode: str = "NORMAL"
    quarter_step: int = 0
    quarter_base_cash: float = 0.0
    cum_buy_amount: float = 0.0
    cum_sell_amount: float = 0.0


def _exec_infinite(o, px: dict):
    """strategies/infinite/simulate.py check_execution 과 동일."""
    close, low, high = px["close"], px["low"], px["high"]
    if o.order_type == "MOC":
        return {"price": close, "qty": o.qty}
    if o.order_type == "LOC":
        if o.side == "buy" and close <= o.price:
            return {"price": close, "qty": o.qty}
        if o.side == "sell" and close >= o.price:
            return {"price": close, "qty": o.qty}
    elif o.order_type == "LIMIT":
        if o.side == "buy" and low <= o.price:
            return {"price": o.price, "qty": o.qty}
        if o.side == "sell" and high >= o.price:
            return {"price": o.price, "qty": o.qty}
    return None


def _run_infinite(p: Params, rows: list[dict]) -> Result:
    from strategies.infinite.trading_logic import (
        generate_orders, calc_T_from_avg, calc_star_pct)

    res = Result(params=asdict(p))
    pf, st = _PF(p), _ST(star_pct=p.R)
    cash, fee = p.seed, p.fee
    cyc_buy = cyc_sell = 0.0
    cyc_start = None
    prev_close = None
    blocked = 0
    blocked_by: dict = {}
    max_T = 0.0
    qt_entries = 0

    def fill(ex, side):
        nonlocal st
        price, qty = ex["price"], ex["qty"]
        amount = round(price * qty, 2)
        if side == "buy":
            st.qty += qty
            st.avg_price = round((st.avg_price * (st.qty - qty) + amount) / st.qty, 4) \
                if st.qty > 0 else 0.0
            st.cum_buy_amount += amount
        else:
            st.qty = max(0, st.qty - qty)
            st.cum_sell_amount += amount
            if st.qty == 0:
                st.avg_price = 0.0
        st.T = calc_T_from_avg(st.avg_price, st.qty, pf.B)
        st.star_pct = calc_star_pct(st.T, pf.A, pf.R)

    for r in rows:
        close, today = r["close"], r["date"].replace("-", "")

        if st.qty <= 0 and st.cum_buy_amount <= 0:
            q = max(1, int(pf.B / close)) if close > 0 else 0
            gross = close * q
            need = round(gross * (1 + fee), 2)
            if q > 0 and need <= cash:
                st.avg_price, st.qty = close, q
                st.cum_buy_amount, st.cum_sell_amount = round(gross, 2), 0.0
                st.T = calc_T_from_avg(st.avg_price, st.qty, pf.B)
                st.star_pct = calc_star_pct(st.T, pf.A, pf.R)
                st.mode, st.quarter_step, st.quarter_base_cash = "NORMAL", 0, 0.0
                cash -= need
                cyc_buy, cyc_sell = need, 0.0
                cyc_start = r["date"]
                res.trades.append({"date": r["date"], "side": "buy", "type": "최초매수",
                                   "price": round(close, 4), "qty": q,
                                   "amount": round(gross, 2),
                                   "fee": round(gross * fee, 2), "note": "시장가"})
            elif q > 0:
                blocked += 1
            res.equity.append((r["date"], round(cash + st.qty * close, 2)))
            prev_close = close
            continue

        orders = generate_orders(pf, st, today, prev_close)
        fills = []
        for o in orders:
            ex = _exec_infinite(o, r)
            if not ex:
                continue
            gross = ex["price"] * ex["qty"]
            if o.side == "buy":
                need = round(gross * (1 + fee), 2)
                if need > cash:
                    blocked += 1
                    key = (f"QUARTER(step{st.quarter_step})" if st.mode == "QUARTER"
                           else f"NORMAL(T{int(st.T // 5) * 5}~)")
                    blocked_by[key] = blocked_by.get(key, 0) + 1
                    continue
                cash -= need
                cyc_buy += need
            else:
                got = round(gross * (1 - fee), 2)
                cash += got
                cyc_sell += got
            fill(ex, o.side)
            fills.append((o.side, o.order_type))
            res.trades.append({"date": r["date"], "side": o.side, "type": o.order_type,
                               "price": round(ex["price"], 4), "qty": ex["qty"],
                               "amount": round(gross, 2), "fee": round(gross * fee, 2),
                               "note": f"T={st.T} ☆{st.star_pct:.1f}%"})

        max_T = max(max_T, st.T)

        if st.mode == "QUARTER" and 1 <= st.quarter_step <= 10:
            if any(s == "sell" and t == "LOC" for s, t in fills):
                st.mode, st.quarter_step, st.quarter_base_cash = "NORMAL", 0, 0.0
        if st.mode == "QUARTER" and fills:
            if st.quarter_step == 0:
                st.quarter_step = 1
                # 운영 worker.py:826 과 동일 — min(B, 실예수금/10).
                # (simulate.py 는 현금을 추적하지 않아 cum_sell×0.3/10 을 대용으로 쓴다.
                #  백테스트는 예수금을 추적하므로 운영 공식을 따른다.)
                st.quarter_base_cash = min(pf.B, cash / 10) if cash > 0 else 0.0
                if st.quarter_base_cash <= 0:
                    st.quarter_base_cash = pf.B * 0.5
            elif 1 <= st.quarter_step <= 10:
                st.quarter_step += 1
                if st.quarter_step > 10:
                    st.quarter_step = 0
        if st.mode != "QUARTER" and 39.1 <= st.T <= 40:
            st.mode, st.quarter_step, st.quarter_base_cash = "QUARTER", 0, 0.0
            qt_entries += 1

        if st.qty == 0 and st.cum_buy_amount > 0:
            profit = cyc_sell - cyc_buy
            res.cycles.append({
                "n": len(res.cycles) + 1, "start": cyc_start, "end": r["date"],
                "buy": round(cyc_buy, 2), "sell": round(cyc_sell, 2),
                "profit": round(profit, 2),
                "profit_pct": round(profit / cyc_buy * 100, 2) if cyc_buy else 0.0,
                "days": _daydiff(cyc_start, r["date"]), "seed": round(pf.seed, 2),
            })
            st = _ST(star_pct=p.R)
            cyc_buy = cyc_sell = 0.0
            if p.compound:
                pf.seed = p.seed + sum(c["profit"] for c in res.cycles)

        res.equity.append((r["date"], round(cash + st.qty * close, 2)))
        prev_close = close

    res.meta.update({"max_T": max_T, "quarter_entries": qt_entries,
                     "cash_blocked_days": blocked,
                     "cash_blocked_by": blocked_by,
                     "open_qty": st.qty, "open_avg": round(st.avg_price, 4),
                     "open_T": st.T, "open_mode": st.mode,
                     "final_B": round(pf.seed / pf.A, 2) if pf.A else 0})
    if blocked:
        res.warnings.append(
            f"예수금 부족으로 거부된 매수 {blocked}건. 후반전은 ☆%가 음수로 내려가(T=30에 "
            f"R-15%p) 1/4을 손실로 매도하므로 예수금이 규칙상 필요액 밑으로 떨어질 수 있습니다. "
            f"실계좌에서도 KIS가 거부하는 상황이라 그대로 반영했습니다. 발생 구간: "
            + ", ".join(f"{k} {v}건" for k, v in sorted(blocked_by.items(), key=lambda x: -x[1])))
    return res


# ══════════════════ 떨사오팔 / 종사종팔 (트렌치 공통) ══════════════════
class _TK:
    """Ticker 모델 대역. generate_orders 가 읽는 필드만 갖는다."""
    def __init__(self, p: Params):
        self.ticker = p.ticker
        self.total_usd = p.seed
        self.num_tranches = p.num_tranches
        self.x_pct = p.x_pct
        self.loss_cut_days = p.loss_cut_days
        self.seed_reflect_enabled = p.seed_reflect
        self.current_cycle = 1
        self.compound_add = 0.0


@dataclass
class _TR:
    id: int
    tranche_num: int
    status: str = "IDLE"
    avg_price: float = 0.0
    qty: int = 0
    buy_price: float = 0.0
    buy_date: str = ""
    days_held: int = 0
    cycle_number: int = 1


def _exec_tranche(o, px: dict):
    """ddsop·jongsa simulate.py check_execution 과 동일 (LIMIT 없음)."""
    close = px["close"]
    if o.order_type == "MOC":
        return {"price": close, "qty": o.qty}
    if o.order_type == "LOC":
        if o.side == "buy" and close <= o.price:
            return {"price": close, "qty": o.qty}
        if o.side == "sell" and close >= o.price:
            return {"price": close, "qty": o.qty}
    return None


def _run_tranche(p: Params, rows: list[dict]) -> Result:
    if p.strategy == "ddsop":
        from strategies.ddsop.trading_logic import generate_orders
    else:
        from strategies.jongsa.trading_logic import generate_orders

    res = Result(params=asdict(p))
    tk = _TK(p)
    trs = [_TR(id=i, tranche_num=i) for i in range(1, p.num_tranches + 1)]
    by_id = {t.id: t for t in trs}
    cash, fee = p.seed, p.fee
    cyc_buy = cyc_sell = cyc_pnl = 0.0
    cyc_start = None
    prev_close = None
    blocked = 0
    losscuts = 0
    max_bought = 0

    for r in rows:
        close, today = r["close"], r["date"].replace("-", "")
        if prev_close is None:          # 첫날은 전일종가가 없어 주문 불가
            prev_close = close
            res.equity.append((r["date"], round(cash, 2)))
            continue

        orders = generate_orders(tk, trs, prev_close, today, cash)
        day_loc_sold_nums = []
        for o in orders:
            ex = _exec_tranche(o, r)
            if not ex:
                continue
            t = by_id.get(o.tranche_id)
            if t is None:
                continue
            gross = ex["price"] * ex["qty"]
            if o.side == "buy":
                need = round(gross * (1 + fee), 2)
                if need > cash:
                    blocked += 1
                    continue
                cash -= need
                cyc_buy += need
                if cyc_start is None:
                    cyc_start = r["date"]
                t.status = "BOUGHT"
                t.avg_price = t.buy_price = ex["price"]
                t.qty = ex["qty"]
                t.buy_date = today
                t.days_held = 0
                res.trades.append({"date": r["date"], "side": "buy", "type": o.order_type,
                                   "price": round(ex["price"], 4), "qty": ex["qty"],
                                   "amount": round(gross, 2), "fee": round(gross * fee, 2),
                                   "note": f"T{t.tranche_num} · {o.desc}"})
            else:
                got = round(gross * (1 - fee), 2)
                pnl = round(got - (t.avg_price * t.qty), 2)
                cash += got
                cyc_sell += got
                cyc_pnl += pnl
                if o.order_type == "MOC":
                    losscuts += 1
                else:
                    day_loc_sold_nums.append(t.tranche_num)
                # 종사종팔4 익절복리: 이익난 매도만 '수익 ÷ 트렌치수' 누적 (손실로는 안 깎음)
                if p.v4_compound and p.strategy == "jongsa" and pnl > 0:
                    tk.compound_add = round(
                        tk.compound_add + pnl / max(1, p.num_tranches), 2)
                res.trades.append({"date": r["date"], "side": "sell", "type": o.order_type,
                                   "price": round(ex["price"], 4), "qty": ex["qty"],
                                   "amount": round(gross, 2), "fee": round(gross * fee, 2),
                                   "note": f"T{t.tranche_num} · {o.desc} · 손익 ${pnl:+,.2f}"})
                t.status = "IDLE"
                t.avg_price = t.buy_price = 0.0
                t.qty = 0
                t.buy_date = ""
                t.days_held = 0

        # 싸이클 종료: T1 이 오늘 LOC(익절)로 비었을 때만 (simulate.py 와 동일)
        if 1 in day_loc_sold_nums:
            res.cycles.append({
                "n": len(res.cycles) + 1, "start": cyc_start or r["date"], "end": r["date"],
                "buy": round(cyc_buy, 2), "sell": round(cyc_sell, 2),
                "profit": round(cyc_pnl, 2),
                "profit_pct": round(cyc_pnl / cyc_buy * 100, 2) if cyc_buy else 0.0,
                "days": _daydiff(cyc_start or r["date"], r["date"]),
                "seed": round(tk.total_usd + tk.compound_add * p.num_tranches, 2),
            })
            cyc_buy = cyc_sell = cyc_pnl = 0.0
            cyc_start = None
            tk.current_cycle += 1
            for t in trs:
                t.cycle_number = tk.current_cycle

        for t in trs:                   # 보유일수 증가 (장 마감 후)
            if t.status == "BOUGHT":
                t.days_held += 1
        max_bought = max(max_bought, sum(1 for t in trs if t.status == "BOUGHT"))

        held = sum(t.qty for t in trs)
        res.equity.append((r["date"], round(cash + held * close, 2)))
        prev_close = close

    held = sum(t.qty for t in trs)
    res.meta.update({
        "loss_cuts": losscuts, "max_tranches_held": max_bought,
        "cash_blocked_days": blocked, "open_qty": held,
        "open_tranches": [{"n": t.tranche_num, "qty": t.qty,
                           "price": round(t.buy_price, 4), "days": t.days_held}
                          for t in trs if t.status == "BOUGHT"],
        "compound_add": round(tk.compound_add, 2),
        "amt_per_tranche": round(p.seed / max(1, p.num_tranches) + tk.compound_add, 2),
    })
    if blocked:
        res.warnings.append(f"예수금 부족으로 거부된 매수 {blocked}건 — 시드가 트렌치 설정에 비해 작습니다.")
    return res


def _daydiff(a: str, b: str) -> int:
    from datetime import date
    try:
        ya, ma, da = (int(x) for x in a.split("-"))
        yb, mb, db = (int(x) for x in b.split("-"))
        return (date(yb, mb, db) - date(ya, ma, da)).days
    except Exception:
        return 0


# ══════════════════ 엔트리포인트 ══════════════════
def run(p: Params, rows: list[dict], data_meta: dict | None = None) -> Result:
    if p.strategy not in STRATEGIES:
        raise ValueError(f"알 수 없는 전략: {p.strategy} (가능: {', '.join(STRATEGIES)})")
    if len(rows) < 2:
        raise ValueError("거래일이 2일 미만입니다. 기간을 늘리세요.")
    res = _run_infinite(p, rows) if p.strategy == "infinite" else _run_tranche(p, rows)
    res.meta.update(data_meta or {})
    res.meta["strategy_name"] = STRATEGIES[p.strategy]
    _metrics(res, rows, p.seed)
    if res.metrics["cycles"] == 0:
        res.warnings.append("완료된 싸이클이 0건입니다. 기간이 짧거나 목표수익률이 높아 "
                            "한 번도 전량청산되지 않았습니다.")
    return res
