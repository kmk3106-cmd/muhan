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
    "vr": "VR 밸류리밸런싱 5.0",
}

# VR G값별 평균 현금보유 (사양서 §2 'G 값 가이드'). 초기 Pool 비율 기본값으로 쓴다 —
# 정상상태 근처에서 시작해야 초기 과도기가 결과를 지배하지 않는다.
VR_CASH_BY_G = {10: 13.2, 20: 21.0, 30: 27.0, 40: 31.84}


def vr_default_pool_pct(g: float) -> float:
    """G 에 대응하는 평균 현금보유%(선형보간)."""
    ks = sorted(VR_CASH_BY_G)
    if g <= ks[0]:
        return VR_CASH_BY_G[ks[0]]
    if g >= ks[-1]:
        return VR_CASH_BY_G[ks[-1]]
    for a, b in zip(ks, ks[1:]):
        if a <= g <= b:
            t = (g - a) / (b - a)
            return round(VR_CASH_BY_G[a] + t * (VR_CASH_BY_G[b] - VR_CASH_BY_G[a]), 2)
    return 20.0

# 운영 중인 설정 (서버 DB is_active=1 기준, 프리셋 기본값으로 쓴다)
PRESETS = {
    "infinite": {"SOXL": {"seed": 5000, "A": 40, "R": 12.0},
                 "TQQQ": {"seed": 5000, "A": 40, "R": 10.0}},
    "ddsop": {"TECL": {"seed": 12524.45, "num_tranches": 7, "x_pct": 0.5, "loss_cut_days": 40},
              "SPXL": {"seed": 12500.0, "num_tranches": 7, "x_pct": 0.5, "loss_cut_days": 40}},
    "jongsa": {"UPRO": {"seed": 15000.0, "num_tranches": 7, "x_pct": 2.7, "loss_cut_days": 7}},
}

# VR 은 같은 티커(TQQQ)에 기수가 여러 개라 티커 키로 못 쓴다 — 기수별 프리셋으로 둔다.
VR_PRESETS = {
    "0기 거치식 (운영)": {"vr_unit": 4, "vr_g": 16.0, "vr_buy_limit_pct": 25.0,
                         "vr_sell_steps": 11, "vr_cashflow": 0.0},
    "5기 인출식 (운영)": {"vr_unit": 2, "vr_g": 41.0, "vr_buy_limit_pct": 10.0,
                         "vr_sell_steps": 14, "vr_cashflow": -75.0},
    "적립식 예시 (G10)": {"vr_unit": 4, "vr_g": 10.0, "vr_buy_limit_pct": 75.0,
                         "vr_sell_steps": 11, "vr_cashflow": 250.0},
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
    # VR 밸류리밸런싱 (2주 주기)
    vr_unit: int = 4                  # 모델 단위 수량 (0기 4, 5기 2)
    vr_g: float = 16.0                # G — 26주마다 +1 (자동)
    vr_buy_limit_pct: float = 25.0    # 주기당 Pool 사용 한도 % (적립75/거치50/인출25 계열)
    vr_sell_steps: int = 11           # 매도 사다리 단수 (기수별 설정값)
    vr_cashflow: float = 0.0          # 주기당 적립(+)/인출(−)
    vr_pool_pct: float = -1.0         # 초기 Pool 비율 %. 음수면 G 기준 기본값
    # G 증가 주기(주). 사양서가 엇갈린다: §6.9·DB주석 "26주마다 +1",
    # §2 가이드 "1년 단위로 /11→/12". 0 이면 G 고정.
    vr_g_step_weeks: int = 26

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


def _buyhold(rows: list[dict], seed: float,
             flows: list[tuple[int, float]] | None = None) -> tuple[list, float]:
    """첫날 종가에 시드 전액 매수 후 보유.

    flows 가 있으면 같은 시점에 같은 금액을 똑같이 적립(+)·인출(−)한다.
    적립식 VR 을 '그냥 사서 모으기'와 공정하게 비교하려면 벤치마크도 입금을 받아야 한다.
    """
    p0 = rows[0]["close"]
    if p0 <= 0:
        return [], seed
    sh = seed / p0
    fmap: dict[int, float] = {}
    for i, amt in (flows or []):
        fmap[i] = fmap.get(i, 0.0) + amt
    eq = []
    for i, r in enumerate(rows):
        amt = fmap.get(i)
        if amt and r["close"] > 0:
            d = amt / r["close"]
            sh = max(0.0, sh + d)          # 인출이 보유를 넘으면 0에서 멈춘다
        eq.append((r["date"], sh * r["close"]))
    return eq, eq[-1][1]


def _metrics(res: Result, rows: list[dict], seed: float) -> None:
    eq = res.equity
    final = eq[-1][1] if eq else seed
    yrs = max(len(rows) / 252.0, 1e-9)
    wins = sum(1 for c in res.cycles if c["profit"] > 0)
    realized = sum(c["profit"] for c in res.cycles)
    flows = res.meta.get("flows") or []
    # 적립과 인출이 섞이면 '순투입'만으로는 수익률이 왜곡된다(인출식은 분모가 0에 수렴).
    # 투입 = 시드 + 누적적립, 회수 = 최종자산 + 누적인출 로 잡는다.
    deposits = sum(a for _, a in flows if a > 0)
    withdrawals = -sum(a for _, a in flows if a < 0)
    invested = seed + deposits
    returned = final + withdrawals
    bh_eq, bh_final = _buyhold(rows, seed, flows)
    bh_returned = bh_final + withdrawals
    m = {
        "invested": round(invested, 2),
        "withdrawn": round(withdrawals, 2),
        "deposited": round(deposits, 2),
        "net_invested": round(seed + sum(a for _, a in flows), 2),
        "cashflow_total": round(sum(a for _, a in flows), 2),
        "final_equity": round(final, 2),
        "total_return_pct": round((returned / invested - 1) * 100, 2),
        "cagr_pct": round(((returned / invested) ** (1 / yrs) - 1) * 100, 2),
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
        "bh_return_pct": round((bh_returned / invested - 1) * 100, 2),
        "bh_cagr_pct": round(((bh_returned / invested) ** (1 / yrs) - 1) * 100, 2),
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
    blocked: list[float] = []      # 거부된 매수의 부족액
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
                    # 수량은 전일종가로 계산되고 체결은 당일종가로 된다. 그날 주가가
                    # 오르면 몇 달러 모자라 거부된다. 예수금이 1주 값도 안 될 때도
                    # 규칙이 최소 1주를 주문하므로(max(1, ...)) 같은 일이 생긴다.
                    blocked.append(round(need - cash, 2))
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
        "cash_blocked_days": len(blocked), "open_qty": held,
        "cash_blocked_median": (round(sorted(blocked)[len(blocked) // 2], 2)
                                if blocked else 0.0),
        "cash_blocked_max": round(max(blocked), 2) if blocked else 0.0,
        "open_tranches": [{"n": t.tranche_num, "qty": t.qty,
                           "price": round(t.buy_price, 4), "days": t.days_held}
                          for t in trs if t.status == "BOUGHT"],
        "compound_add": round(tk.compound_add, 2),
        "amt_per_tranche": round(p.seed / max(1, p.num_tranches) + tk.compound_add, 2),
    })
    if blocked:
        med = sorted(blocked)[len(blocked) // 2]
        res.warnings.append(
            f"예수금이 모자라 못 산 날 {len(blocked)}일 (부족액 중앙값 ${med:,.2f}, "
            f"최대 ${max(blocked):,.2f}). 매수 수량은 전일종가로 계산하고 체결은 당일종가로 "
            f"되기 때문에, 그날 주가가 오르면 몇 달러가 모자라 주문이 거부됩니다. "
            f"1회 매수금액이 예수금 전부에 가까울 때 자주 생기고"
            + (" — 익절복리를 켜면 매수금액이 계속 커져 그 상태가 됩니다." if p.v4_compound
               else "(시드를 올리거나 트렌치를 늘리면 줄어듭니다).")
            + " 실계좌에서도 KIS가 거부하는 상황이라 그대로 반영했습니다.")
    return res


# ══════════════════ VR 밸류리밸런싱 5.0 ══════════════════
def _vr_cycles(dates: list[str]) -> list[tuple[int, int]]:
    """거래일 목록을 라오어 2주 주기로 자른다.

    주기 종료는 금요일, 다음 주기 시작은 그 다음 월요일(직전종료+3), 종료는 직전종료+14.
    따라서 주기 구간은 [종료−11일, 종료] = 월요일~2주 뒤 금요일.
    반환: 각 주기의 (시작 인덱스, 종료 인덱스) — 거래일이 없는 주기는 건너뛴다.
    """
    from datetime import datetime, timedelta
    d0 = datetime.strptime(dates[0], "%Y-%m-%d")
    dn = datetime.strptime(dates[-1], "%Y-%m-%d")
    # 첫 금요일을 기준 종료일로
    end = d0 + timedelta(days=(4 - d0.weekday()) % 7)
    out = []
    while end <= dn:
        s, e = (end - timedelta(days=11)).strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        idx = [i for i, d in enumerate(dates) if s <= d <= e]
        if idx:
            out.append((idx[0], idx[-1]))
        end += timedelta(days=14)
    return out


def _run_vr(p: Params, rows: list[dict]) -> Result:
    """운영 strategies.vr.vr_logic 의 공식을 그대로 사용한다.

    주기마다 ①다음V ②Pool이월 ③밴드 ④매수사다리 ⑤매도사다리 를 산출하고,
    2주 예약주문이 살아 있는 동안 가격이 닿으면 체결시킨다.
    가격·수량은 전부 '모델' 기준이다(배수는 주문수량에만 곱하므로 수익률은 동일).
    """
    from strategies.vr.vr_logic import next_v, bands, buy_ladder, sell_ladder, r2

    res = Result(params=asdict(p))
    dates = [r["date"] for r in rows]
    cyc = _vr_cycles(dates)
    if len(cyc) < 2:
        raise ValueError("VR 은 2주 주기 전략입니다. 최소 4주 이상 기간을 잡으세요.")

    pool_pct = p.vr_pool_pct if p.vr_pool_pct >= 0 else vr_default_pool_pct(p.vr_g)
    pool = r2(p.seed * pool_pct / 100.0)
    px0 = rows[cyc[0][0]]["close"]
    qty = int((p.seed - pool) / px0)          # 모델 잔여
    if qty <= 0:
        raise ValueError(f"시드 ${p.seed:,.0f} 로는 {p.ticker} 1주도 못 삽니다 "
                         f"(시작가 ${px0:,.2f}).")
    pool = r2(p.seed - qty * px0)             # 못 산 끝돈은 Pool 로
    v = r2(qty * px0)                         # V₀ = 초기 평가금
    g = float(p.vr_g)
    fee = p.fee
    week = 0
    buys_done = sells_done = 0
    cash_short = 0
    hist = []
    flows: list[tuple[int, float]] = []

    # 첫 주기 시작 전 구간은 보유만
    for r in rows[:cyc[0][0]]:
        res.equity.append((r["date"], round(qty * r["close"] + pool, 2)))

    for ci, (a, b) in enumerate(cyc):
        e_val = r2(qty * rows[a]["close"])     # 직전 주기 마감 평가금 자리 (첫 주기는 시작가)
        if ci > 0:
            e_val = r2(qty * rows[cyc[ci - 1][1]]["close"])
            v = next_v(v, pool, g, e_val, p.vr_cashflow)
            pool = r2(pool + p.vr_cashflow)
            if p.vr_cashflow:
                flows.append((a, float(p.vr_cashflow)))
        lo, hi = bands(v)
        ladder_b = buy_ladder(lo, qty, p.vr_unit, pool, p.vr_buy_limit_pct)
        ladder_s = sell_ladder(hi, qty, p.vr_unit, p.vr_sell_steps, pool)
        open_b = [x["price"] for x in ladder_b]          # 높은 가격부터 닿는다
        open_s = [x["price"] for x in ladder_s]
        c_buy = c_sell = 0
        c_bamt = c_samt = 0.0

        for r in rows[a:b + 1]:
            # 매수: 하락하며 높은 쪽 호가부터 체결
            for price in sorted([x for x in open_b if r["low"] <= x], reverse=True):
                cost = round(p.vr_unit * price * (1 + fee), 2)
                if cost > pool:
                    cash_short += 1
                    continue
                pool = r2(pool - cost)
                qty += p.vr_unit
                open_b.remove(price)
                c_buy += 1
                c_bamt += cost
                buys_done += 1
                res.trades.append({"date": r["date"], "side": "buy", "type": "예약",
                                   "price": round(price, 4), "qty": p.vr_unit,
                                   "amount": round(p.vr_unit * price, 2),
                                   "fee": round(p.vr_unit * price * fee, 2),
                                   "note": f"{week}주차 매수점 · 잔여 {qty} · Pool ${pool:,.0f}"})
            # 매도: 상승하며 낮은 쪽 호가부터 체결
            for price in sorted([x for x in open_s if r["high"] >= x]):
                if qty - p.vr_unit < 0:
                    break
                got = round(p.vr_unit * price * (1 - fee), 2)
                pool = r2(pool + got)
                qty -= p.vr_unit
                open_s.remove(price)
                c_sell += 1
                c_samt += got
                sells_done += 1
                res.trades.append({"date": r["date"], "side": "sell", "type": "예약",
                                   "price": round(price, 4), "qty": p.vr_unit,
                                   "amount": round(p.vr_unit * price, 2),
                                   "fee": round(p.vr_unit * price * fee, 2),
                                   "note": f"{week}주차 매도점 · 잔여 {qty} · Pool ${pool:,.0f}"})
            res.equity.append((r["date"], round(qty * r["close"] + pool, 2)))

        # 주기 결산 — VR 은 '싸이클 손익' 개념이 없어 주기 기록으로 남긴다
        eq_end = round(qty * rows[b]["close"] + pool, 2)
        hist.append({
            "n": ci + 1, "week": week, "start": rows[a]["date"], "end": rows[b]["date"],
            "v": v, "band_lo": lo, "band_hi": hi, "qty": qty, "pool": round(pool, 2),
            "eval": round(qty * rows[b]["close"], 2), "equity": eq_end,
            "buys": c_buy, "sells": c_sell, "g": g,
            "ladder_b": len(ladder_b), "ladder_s": len(ladder_s),
        })
        week += 2
        step = int(p.vr_g_step_weeks or 0)
        if step > 0 and week and week % step == 0:
            g += 1

    for r in rows[cyc[-1][1] + 1:]:
        res.equity.append((r["date"], round(qty * r["close"] + pool, 2)))

    res.meta.update({
        "flows": flows,
        "vr_cycles": len(cyc), "weeks": week, "g_start": p.vr_g, "g_end": g,
        "pool_pct_used": pool_pct,
        "v_final": v, "qty_final": qty, "pool_final": round(pool, 2),
        "buys": buys_done, "sells": sells_done,
        "cash_blocked_days": cash_short,
        "open_qty": qty,
        "history": hist[-40:],
        "cash_ratio_final": (round(pool / (pool + qty * rows[-1]["close"]) * 100, 1)
                             if (pool + qty * rows[-1]["close"]) > 0 else None),
    })
    if cash_short:
        res.warnings.append(
            f"Pool 이 모자라 못 산 매수점 {cash_short}건. VR 은 Pool 소진 시 하락장에도 "
            f"매수하지 않고 대기하는 것이 규칙입니다(사양서 §3-4). 매수한도%를 낮추거나 "
            f"G 를 키우면 Pool 여유가 늘어납니다.")
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
    if p.strategy == "infinite":
        res = _run_infinite(p, rows)
    elif p.strategy == "vr":
        res = _run_vr(p, rows)
    else:
        res = _run_tranche(p, rows)
    res.meta.update(data_meta or {})
    res.meta["strategy_name"] = STRATEGIES[p.strategy]
    _metrics(res, rows, p.seed)
    if p.strategy == "vr":
        # VR 은 전량청산 개념이 없어 싸이클 승률이 성립하지 않는다
        res.metrics.update({"cycles": res.meta["vr_cycles"], "wins": None,
                            "losses": None, "win_rate": None,
                            "avg_cycle_days": 14, "realized": None})
    elif res.metrics["cycles"] == 0:
        res.warnings.append("완료된 싸이클이 0건입니다. 기간이 짧거나 목표수익률이 높아 "
                            "한 번도 전량청산되지 않았습니다.")
    return res
