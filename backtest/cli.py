# -*- coding: utf-8 -*-
"""백테스트 CLI.

  python -m backtest.cli --strategy infinite --ticker TQQQ --seed 5000 --start 2020-01-01
  python -m backtest.cli --strategy jongsa --ticker UPRO --preset
  python -m backtest.cli --cost SOXL          # 다운로드 비용 견적만
  python -m backtest.cli --list               # 캐시된 티커
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import data as D                      # noqa: E402
from backtest.engine import Params, PRESETS, STRATEGIES, run   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _fmt(v, w=12):
    return f"${v:>{w},.2f}"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="무한매수법·떨사오팔·종사종팔4 백테스트",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", "-s", default="infinite", choices=list(STRATEGIES))
    ap.add_argument("--ticker", "-t", default="TQQQ")
    ap.add_argument("--start", default="2018-05-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--seed", type=float, default=5000.0)
    ap.add_argument("--fee", type=float, default=0.0, help="편도 수수료 %% (0.25 = 0.25%%)")
    ap.add_argument("--A", type=int, default=40, help="[무한] 분할수")
    ap.add_argument("--R", type=float, default=10.0, help="[무한] 목표수익률 %%")
    ap.add_argument("--compound", action="store_true", help="[무한] 복리모드")
    ap.add_argument("--tranches", type=int, default=7, help="[떨사/종사] 트렌치수")
    ap.add_argument("--x", type=float, default=2.7, help="[떨사/종사] x%%")
    ap.add_argument("--losscut", type=int, default=40, help="[떨사/종사] 손절 거래일")
    ap.add_argument("--seed-reflect", action="store_true", help="[떨사/종사] 씨드반영")
    ap.add_argument("--v4-compound", action="store_true", help="[종사] 익절복리")
    ap.add_argument("--vr-unit", type=int, default=4, help="[VR] 모델 단위 수량")
    ap.add_argument("--vr-g", type=float, default=16.0, help="[VR] G")
    ap.add_argument("--vr-limit", type=float, default=25.0, help="[VR] Pool 사용한도 %%")
    ap.add_argument("--vr-sell-steps", type=int, default=11, help="[VR] 매도 단수")
    ap.add_argument("--vr-cashflow", type=float, default=0.0, help="[VR] 주기당 적립+/인출-")
    ap.add_argument("--vr-pool-pct", type=float, default=-1.0, help="[VR] 초기 Pool %% (-1=자동)")
    ap.add_argument("--vr-g-step", type=int, default=26, help="[VR] G +1 주기(주), 0=고정")
    ap.add_argument("--preset", action="store_true", help="운영 설정값으로 덮어쓰기")
    ap.add_argument("--refresh", action="store_true", help="가격 캐시 강제 갱신")
    ap.add_argument("--trades", type=int, default=0, help="최근 N건 체결 출력")
    ap.add_argument("--cost", metavar="TICKER", help="다운로드 비용 견적만 조회")
    ap.add_argument("--list", action="store_true", help="캐시된 티커 목록")
    a = ap.parse_args(argv)

    if a.list:
        tk = D.cached_tickers()
        print("캐시된 티커:", ", ".join(tk) if tk else "(없음)")
        return 0
    if a.cost:
        c = D.cost(a.cost)
        print(f"{c['ticker']}  dataset={c['dataset']}  "
              f"레코드 {c['records']:,}건  예상비용 ${c['cost_usd']:.4f}")
        return 0

    p = Params(strategy=a.strategy, ticker=a.ticker.upper(), start=a.start,
               end=a.end or "", seed=a.seed, fee_pct=a.fee,
               A=a.A, R=a.R, compound=a.compound,
               num_tranches=a.tranches, x_pct=a.x, loss_cut_days=a.losscut,
               seed_reflect=a.seed_reflect, v4_compound=a.v4_compound,
               vr_unit=a.vr_unit, vr_g=a.vr_g, vr_buy_limit_pct=a.vr_limit,
               vr_sell_steps=a.vr_sell_steps, vr_cashflow=a.vr_cashflow,
               vr_pool_pct=a.vr_pool_pct, vr_g_step_weeks=a.vr_g_step)
    if a.preset:
        pre = PRESETS.get(p.strategy, {}).get(p.ticker)
        if not pre:
            print(f"[경고] {STRATEGIES[p.strategy]} / {p.ticker} 운영 프리셋이 없습니다.")
        else:
            for k, v in pre.items():
                setattr(p, "seed" if k == "seed" else k, v)
            print(f"운영 프리셋 적용: {pre}")

    rows, dmeta = D.load(p.ticker, p.start or None, p.end or None, refresh=a.refresh)
    res = run(p, rows, dmeta)
    m, meta = res.metrics, res.meta

    print()
    print("=" * 74)
    print(f" {meta['strategy_name']}  |  {p.ticker}  |  {dmeta['range_used'][0]} ~ "
          f"{dmeta['range_used'][1]}  ({m['years']}년, {dmeta['days']}거래일)")
    print("=" * 74)
    if p.strategy == "infinite":
        print(f" 시드 {_fmt(p.seed)}   A={p.A}분할  B={_fmt(p.seed/p.A, 8)}  "
              f"R={p.R}%  복리={'ON' if p.compound else 'OFF'}")
    elif p.strategy == "vr":
        print(f" 시드 {_fmt(p.seed)}   단위 {p.vr_unit}주  "
              f"G {meta['g_start']:.0f}→{meta['g_end']:.0f}  "
              f"매수한도 {p.vr_buy_limit_pct}%  매도 {p.vr_sell_steps}단  "
              f"현금흐름 {p.vr_cashflow:+,.0f}/주기")
        print(f"            초기 Pool {meta['pool_pct_used']}%  "
              f"2주 주기 {meta['vr_cycles']}회 ({meta['weeks']}주)")
        if m["deposited"] or m["withdrawn"]:
            print(f"            투입 {_fmt(m['invested'])} (시드 + 적립 {m['deposited']:,.0f})"
                  f"   회수 {_fmt(m['final_equity'] + m['withdrawn'])} "
                  f"(최종자산 + 인출 {m['withdrawn']:,.0f})")
    else:
        print(f" 시드 {_fmt(p.seed)}   트렌치 {p.num_tranches}개  "
              f"1회 {_fmt(p.seed/p.num_tranches, 8)}  x={p.x_pct}%  "
              f"손절 {p.loss_cut_days}일" +
              (f"  익절복리=ON" if p.v4_compound else ""))
    if p.fee_pct:
        print(f" 수수료 편도 {p.fee_pct}%")
    sp = dmeta.get("splits") or []
    print(f" 분할조정 {len(sp)}건 ({dmeta.get('splits_source')})"
          + (": " + ", ".join(f"{s['date']} {s['ratio']:g}:1" for s in sp) if sp else ""))
    print("-" * 74)
    print(f" 최종자산   {_fmt(m['final_equity'])}   수익률 {m['total_return_pct']:+8.2f}%"
          f"   CAGR {m['cagr_pct']:+7.2f}%")
    print(f" 최대낙폭   {m['mdd_pct']:>12.2f}%   위험조정(CAGR/|MDD|) {m['rr']}")
    if p.strategy == "vr":
        print(f" 주기       {m['cycles']:>8}회     매수 {meta['buys']}회 / 매도 {meta['sells']}회"
              f"   (VR 은 전량청산이 없어 승률 개념이 없다)")
        print(f" 체결       {m['trades']:>8}건"
              + (f"   수수료 {_fmt(m['fees_paid'], 8)}" if m['fees_paid'] else ""))
    else:
        print(f" 싸이클     {m['cycles']:>8}건     승 {m['wins']} / 패 {m['losses']}"
              + (f"   승률 {m['win_rate']}%" if m['win_rate'] is not None else "")
              + (f"   평균 {m['avg_cycle_days']}일" if m['avg_cycle_days'] else ""))
        print(f" 실현손익   {_fmt(m['realized'])}   체결 {m['trades']}건"
              + (f"   수수료 {_fmt(m['fees_paid'], 8)}" if m['fees_paid'] else ""))
    print("-" * 74)
    print(f" [비교] 단순보유 {_fmt(m['bh_final'])}  {m['bh_return_pct']:+8.2f}%  "
          f"MDD {m['bh_mdd_pct']:.2f}%  위험조정 {m['bh_rr']}")
    if p.strategy == "vr":
        print(f" [상태] 최종 V {_fmt(meta['v_final'], 10)}  잔여 {meta['qty_final']}주  "
              f"Pool {_fmt(meta['pool_final'], 10)}  현금비중 {meta['cash_ratio_final']}%")
    elif p.strategy == "infinite":
        print(f" [상태] 최대 T={meta['max_T']}  QUARTER 진입 {meta['quarter_entries']}회  "
              f"미청산 {meta['open_qty']}주 (T={meta['open_T']}, {meta['open_mode']})")
    else:
        print(f" [상태] 손절 {meta['loss_cuts']}회  최대 동시보유 {meta['max_tranches_held']}"
              f"/{p.num_tranches}트렌치  미청산 {meta['open_qty']}주"
              + (f"  복리누적 +{_fmt(meta['compound_add'], 8)}/트렌치"
                 if meta.get("compound_add") else ""))
    for w in res.warnings:
        print(f" [!] {w}")

    if p.strategy == "vr" and meta.get("history"):
        print("-" * 74)
        print(" 주기 (최근 10회)")
        print(f" {'주차':>5} {'종료':11s} {'V':>10s} {'하단':>9s} {'상단':>9s} "
              f"{'잔여':>5s} {'Pool':>9s} {'매수':>3s} {'매도':>3s}")
        for h in meta["history"][-10:]:
            print(f" {h['week']:>5} {h['end']:11s} ${h['v']:>9,.0f} ${h['band_lo']:>8,.0f} "
                  f"${h['band_hi']:>8,.0f} {h['qty']:>5} ${h['pool']:>8,.0f} "
                  f"{h['buys']:>3} {h['sells']:>3}")
    if res.cycles:
        print("-" * 74)
        print(" 싸이클 (최근 10건)")
        print(f" {'#':>3} {'시작':11s} {'종료':11s} {'일':>4} {'매수':>12} "
              f"{'손익':>11} {'손익률':>8}")
        for c in res.cycles[-10:]:
            print(f" {c['n']:>3} {c['start']:11s} {c['end']:11s} {c['days']:>4} "
                  f"{_fmt(c['buy'], 11)} {_fmt(c['profit'], 10)} {c['profit_pct']:>7.2f}%")
    if a.trades:
        print("-" * 74)
        print(f" 체결 (최근 {a.trades}건)")
        for t in res.trades[-a.trades:]:
            print(f" {t['date']}  {('매수' if t['side']=='buy' else '매도')} "
                  f"{t['type']:<5} ${t['price']:>9.2f} × {t['qty']:>4}주 = "
                  f"{_fmt(t['amount'], 10)}  {t['note']}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
