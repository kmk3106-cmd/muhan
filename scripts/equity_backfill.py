# -*- coding: utf-8 -*-
"""전체 자산(KIS+NH+토스) 과거 추이 복원 — 증권사 기록에서 역산.

추정이 아니라 **증권사가 돌려주는 실값**으로 만든다.
- KIS  : core/_equity.jsonl 의 일별 마지막 스냅샷 (2026-05-16~)
- NH   : 일별거래내역의 '거래 후 잔고수량(trd_af_bnc_qty)·거래 후 예수금(trd_af_fc_dca)' +
         TQQQ 일별 종가 → 일별 자산. 거래 없는 날은 직전 상태를 이어간다.
- 토스 : **복원하지 않는다**. 1년간 13종목 800건 거래 계좌라 주문내역만으로는 과거 보유가
         재현되지 않고(실측 81주 vs 복원 52주), 과거 예수금 조회 수단도 없다.
         대신 우리가 직접 기록한 스냅샷(core/_toss_series.jsonl, 2026-09-24~)만 사용한다.

계좌별 '추적 시작일'을 함께 기록해, 합산 대상이 늘어난 지점을 화면에서 구분할 수 있게 한다.
주문·취소 API 는 호출하지 않는다(조회 전용).

사용법:
  python scripts/equity_backfill.py            # 계산만 하고 요약 출력
  python scripts/equity_backfill.py --write    # core/_equity_backfill.jsonl 생성
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT_FILE = os.path.join(ROOT, "core", "_equity_backfill.jsonl")

# 복원 시작일. 이보다 앞 구간은 계좌 간 입·출고가 있어 '합산 자산'이 왜곡된다.
# (NH 5기: 2026-05-29 주식 출고 → 2026-07-21 입고. 그 사이 계좌 잔고가 0 이라 자산이
#  사라졌다 돌아온 것처럼 보인다. 돈이 없어진 게 아니라 추적 밖 계좌에 있었다.)
BACKFILL_START = "20260801"
EQUITY_FILE = os.path.join(ROOT, "core", "_equity.jsonl")


def _f(v, d=0.0):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return d


def daily_closes_toss(symbol: str, days: int = 200) -> dict:
    """토스 일봉 → {YYYYMMDD: 종가}. 미국 정규장 공식 종가."""
    from core import toss_client as tc
    out = {}
    r = tc._get(f"/api/v1/candles?symbol={symbol}&interval=1d&count={min(days, 200)}")
    rows = r if isinstance(r, list) else (r.get("candles") or [])
    for c in rows:
        ts = str(c.get("timestamp") or "")[:10].replace("-", "")
        close = _f(c.get("closePrice"))
        if ts and close:
            out[ts] = close
    return out


def nh_daily_state(gid: str) -> tuple[dict, str]:
    """NH 기수의 일별 (보유수량, 예수금) — 거래내역의 거래 후 값 사용."""
    from strategies.vr.config import load_nh_env
    load_nh_env()
    from strategies.vr import nh_client as nh, models as M
    g = M.get_gisu(gid)
    acct, ticker = g["acct_no"], g["ticker"]
    rows = []
    # 하루 20건 초과 시 잘릴 수 있어 월 단위로 끊어 모은다
    start = datetime(2026, 5, 1)
    while start < datetime.now():
        end = min(start + timedelta(days=30), datetime.now())
        try:
            rows += nh.daily_transactions(acct, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "")
        except Exception as e:
            print(f"  [경고] NH {gid} {start:%Y%m%d} 조회 실패: {e}")
        start = end + timedelta(days=1)
    # 하루 20건을 넘겨 잘렸을 수 있는 날은 그 하루만 다시 조회해 보완
    per_day = defaultdict(int)
    for r in rows:
        per_day[str(r.get("trd_dt") or "")[:8]] += 1
    for d, c in list(per_day.items()):
        if c >= 20 and len(d) == 8:
            try:
                rows += nh.daily_transactions(acct, d, d, "")
            except Exception as e:
                print(f"  [경고] NH {gid} {d} 재조회 실패: {e}")
    seen, uniq = set(), []
    for r in rows:
        k = (r.get("trd_dt"), r.get("trd_sno"))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    uniq.sort(key=lambda r: (str(r.get("trd_dt")), int(_f(r.get("trd_sno")))))
    state, first = {}, ""
    qty, cash = None, None
    for r in uniq:
        dt = str(r.get("trd_dt") or "")[:8]
        if len(dt) != 8:
            continue
        # 잔고수량은 그 종목 거래행에만 의미가 있다. 예수금은 계좌 전체라 모든 행에서 갱신
        moved = _f(r.get("trd_qty")) > 0          # 입금·배당 등 수량 0 행은 잔고를 건드리지 않는다
        if (moved and nh.norm_ticker(r.get("iem_cd")) == nh.norm_ticker(ticker)
                and r.get("trd_af_bnc_qty") not in (None, "")):
            qty = int(_f(r.get("trd_af_bnc_qty")))
        if r.get("trd_af_fc_dca") not in (None, ""):
            cash = _f(r.get("trd_af_fc_dca"))
        if qty is not None:
            state[dt] = {"qty": qty, "cash": cash or 0.0}
            first = first or dt

    # ── 실측 기준점 보정
    # 거래내역의 '거래 후 예수금'을 앞에서부터 쌓으면 내역에 행이 남지 않는 현금 이동
    # (실제: vr0 에서 $2,954 — 9/30 배당 이후 거래행이 없는데 예수금이 줄었다)이 그대로
    # 누적된다. 발생 시점을 알 수 없으므로 **현재 증권사 잔고를 기준점으로 잡고** 그 차이를
    # 전 구간에 같은 값으로 되돌린다. 이러면 복원선이 실측선과 이어지는 지점에서 어긋나지 않고,
    # 오차는 과거쪽으로만 남는다.
    if state:
        try:
            bal = nh.balance(acct) or {}
            o0 = bal.get("Output_0") or {}
            real_cash = _f(o0.get("fc_dca"))
            real_qty = 0
            for h in (bal.get("Output_1") or []):
                if nh.norm_ticker(h.get("iem_cd")) == nh.norm_ticker(ticker):
                    real_qty = int(_f(h.get("cns_bse_bnc_qty")))
            last = state[max(state)]
            d_cash = real_cash - last["cash"]
            d_qty = real_qty - last["qty"]
            if abs(d_cash) > 0.01 or d_qty:
                print(f"  [보정] {gid} 실측 기준점: 예수금 {d_cash:+,.2f} / 수량 {d_qty:+d} "
                      f"(복원 ${last['cash']:,.2f}·{last['qty']}주 → 실측 ${real_cash:,.2f}·{real_qty}주)")
                for v in state.values():
                    v["cash"] = round(v["cash"] + d_cash, 2)
                    v["qty"] = max(0, v["qty"] + d_qty)
        except Exception as e:
            print(f"  [경고] {gid} 실측 잔고 조회 실패 — 보정 생략: {e}")
    return state, first


def toss_recorded_daily() -> dict:
    """우리가 기록한 토스 스냅샷(core/_toss_series.jsonl) → {YYYYMMDD: 총자산}. 실측만."""
    path = os.path.join(ROOT, "core", "_toss_series.jsonl")
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            p = json.loads(line)
        except Exception:
            continue
        d = str(p.get("ts") or "")[:10].replace("-", "")
        if d:
            out[d] = _f(p.get("total_assets"))      # 그날 마지막 기록이 남는다
    return out


def kis_daily() -> dict:
    """KIS 일별 마지막 스냅샷 {YYYYMMDD: 총자산}."""
    out = {}
    for line in open(EQUITY_FILE, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            p = json.loads(line)
        except Exception:
            continue
        d = str(p.get("ts") or "")[:10].replace("-", "")
        if d:
            out[d] = _f(p.get("total_assets"))
    return out


def main() -> int:
    write = "--write" in sys.argv
    print("[1] KIS 일별 스냅샷")
    kis = kis_daily()
    print(f"    {len(kis)}일 · {min(kis)} ~ {max(kis)}")

    print("[2] NH 거래내역 → 일별 보유·예수금")
    nh_state, nh_first = {}, ""
    for gid in ("vr0", "vr5"):
        st, first = nh_daily_state(gid)
        nh_state[gid] = st
        nh_first = min(nh_first, first) if nh_first else first
        if st:
            last = st[max(st)]
            print(f"    {gid}: {len(st)}일 · {min(st)}~{max(st)} · 최근 {last['qty']}주 예수금 ${last['cash']:,.2f}")

    print("[3] 토스 기록 스냅샷 (복원 아님 — 실측만)")
    toss_rec = toss_recorded_daily()
    toss_first = min(toss_rec) if toss_rec else ""
    print(f"    {len(toss_rec)}일 · {toss_first or '-'} ~ {max(toss_rec) if toss_rec else '-'}")

    print("[4] TQQQ 일별 종가 (NH 평가용)")
    px_tqqq = daily_closes_toss("TQQQ")
    print(f"    {len(px_tqqq)}일 · {min(px_tqqq)}~{max(px_tqqq)}")

    # ── 합성
    days = [d for d in sorted(kis) if d >= BACKFILL_START]
    print(f"    복원 시작일 {BACKFILL_START} (그 전은 계좌 간 입·출고로 합산 왜곡 — 제외)")
    mults = {}
    try:
        from strategies.vr import models as M
        for g in M.all_gisu():
            mults[g["id"]] = int(g["mult"])
    except Exception:
        mults = {"vr0": 1, "vr5": 6}

    out_rows = []
    # 복원 시작일 '이전'의 마지막 상태를 이어받는다. NH 는 거래 없는 날이 길게 이어져
    # (8월 이후 거래일 드묾) 시작일부터 훑으면 보유·예수금이 0 으로 비어 버린다.
    cur_nh = {}
    for gid, st in nh_state.items():
        prev = [d for d in st if d < BACKFILL_START]
        cur_nh[gid] = st[max(prev)] if prev else None
    # 시작일이 주말이면 그날 종가가 없다 → 직전 거래일 종가로 시작
    _pxp = [d for d in px_tqqq if d < BACKFILL_START]
    last_tqqq = px_tqqq[max(_pxp)] if _pxp else None
    for d in days:
        last_tqqq = px_tqqq.get(d, last_tqqq)
        nh_total, nh_ok = 0.0, False
        for gid, st in nh_state.items():
            if d in st:
                cur_nh[gid] = st[d]
            v = cur_nh.get(gid)
            if v and last_tqqq:
                nh_total += v["qty"] * last_tqqq + v["cash"]   # 평가금 + 실제 예수금
                nh_ok = True
        toss_total = toss_rec.get(d)
        toss_ok = toss_total is not None
        out_rows.append({
            "date": d, "kis": round(kis[d], 2),
            "nh": round(nh_total, 2) if nh_ok else None,
            "toss": round(toss_total, 2) if toss_ok else None,
            "combined": round(kis[d] + nh_total + (toss_total or 0.0), 2),
            "has": "K" + ("N" if nh_ok else "") + ("T" if toss_ok else ""),
        })

    print("\n[5] 복원 결과 (주요 지점)")
    print(f"{'일자':>10} {'KIS':>10} {'NH':>12} {'토스':>10} {'합산':>12}  포함")
    marks = [out_rows[0]]
    for i in range(1, len(out_rows)):
        if out_rows[i]["has"] != out_rows[i - 1]["has"]:
            marks.append(out_rows[i])
    marks += out_rows[-3:]
    for r in marks:
        print(f"{r['date']:>10} {r['kis']:>10,.0f} {(r['nh'] or 0):>12,.0f} {(r['toss'] or 0):>10,.0f} "
              f"{r['combined']:>12,.0f}  {r['has']}")

    # 실측과 대조 (오늘 기록된 합산 스냅샷과 비교)
    try:
        real = [json.loads(l) for l in open(EQUITY_FILE, encoding="utf-8") if '"combined_assets"' in l]
        real = [p for p in real if p.get("combined_assets")]
        if real:
            last = real[-1]
            d = str(last["ts"])[:10].replace("-", "")
            est = next((r for r in out_rows if r["date"] == d), None)
            if est:
                diff = est["combined"] - float(last["combined_assets"])
                print(f"\n[6] 실측 대조 ({d}): 복원 {est['combined']:,.2f} vs 실측 {float(last['combined_assets']):,.2f} "
                      f"→ 차이 {diff:+,.2f} ({abs(diff)/float(last['combined_assets'])*100:.2f}%)")
    except Exception as e:
        print("  대조 실패:", e)

    if write:
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            for r in out_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n저장 완료: {OUT_FILE} ({len(out_rows)}일)")
    else:
        print("\n(미저장 — 저장하려면 --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
