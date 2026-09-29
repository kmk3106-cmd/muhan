# -*- coding: utf-8 -*-
"""보유수량 점검·보정 — 증권사 실체결 기준으로 모델을 맞춘다.

매일 09:00(KST) 루틴에서 실행한다. 기본은 점검(조회)만이고 `--apply` 를 줘야 수정한다.

원칙
- 주문/정정/취소 API 는 호출하지 않는다. 조회 API 와 DB 정정만 한다.
- '증권사 실제 잔고·체결'이 정본이다. 모델(트렌치/상태)을 거기에 맞춘다.
- 근거(체결내역의 주문번호)가 확인된 건만 자동 보정한다. 설명되지 않는 차이는 보고만 한다.
- 무한매수법은 워커가 매 실행 때 잔고에서 평단·수량을 다시 읽어 스스로 맞추므로 보고만 한다.
- 수정 전 DB 를 백업한다.

사용법
  python scripts/qty_reconcile.py            # 점검만
  python scripts/qty_reconcile.py --apply    # 보정까지
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta

# 저장소 루트에서 실행되도록 고정 (scripts/ 에서 실행해도 동작)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LOOKBACK_DAYS = 20
TRANCHE_DBS = [("ddsop", "떨사오팔", "strategies/ddsop/ddsop.db"),
               ("jongsa", "종사종팔", "strategies/jongsa/jongsa.db")]
INFINITE_DB = "strategies/infinite/infinite_buy.db"


def _f(v, d=0.0):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return d


def account_qty(client, ctac) -> dict:
    bal, _ = client.inquire_balance(ctac_tlno=ctac)
    out = {}
    if not bal.empty:
        for _, r in bal.iterrows():
            tk = str(r.get("ovrs_pdno") or "").strip()
            if tk:
                out[tk] = int(_f(r.get("ovrs_cblc_qty")))
    return out


def fills(client, ctac, ticker: str) -> list[dict]:
    """최근 LOOKBACK_DAYS 실체결 (매수/매도)."""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    df = client.inquire_ccnl(pdno=ticker, ord_strt_dt=start, ord_end_dt=end,
                             sll_buy_dvsn="00", ccld_nccs_dvsn="01", ctac_tlno=ctac)
    rows = []
    if df.empty:
        return rows

    def g(r, *ks):
        for k in ks:
            v = r.get(k)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    for _, r in df.iterrows():
        qty = int(_f(g(r, "ft_ccld_qty", "ccld_qty")))
        if qty <= 0:
            continue
        name = g(r, "sll_buy_dvsn_cd_name", "sll_buy_dvsn_cd")
        rows.append({
            "odno": g(r, "odno", "ORGN_ODNO"),
            "side": "sell" if ("매도" in name or name == "01") else "buy",
            "qty": qty,
            "price": _f(g(r, "ft_ccld_unpr3", "ccld_unpr", "ovrs_ord_unpr")),
            "date": g(r, "ord_dt", "dmst_ord_dt", "ccld_dt"),
        })
    return rows


def check_tranche_strategy(key, label, db, client, ctac, acct, apply: bool) -> list[dict]:
    con = sqlite3.connect(db)
    found = []
    for tid, ticker, in con.execute("select id,ticker from tickers where is_active=1"):
        model = sum(int(q or 0) for (q,) in con.execute(
            "select qty from tranches where ticker_id=? and status='BOUGHT'", (tid,)))
        real = acct.get(ticker, 0)
        if model == real:
            continue
        item = {"strategy": label, "ticker": ticker, "model": model, "account": real,
                "diff": real - model, "action": "보고만", "detail": ""}
        known = {o for (o,) in con.execute(
            "select kis_order_no from trade_orders where ticker=? and kis_order_no!=''", (ticker,))}
        missing = [f for f in fills(client, ctac, ticker) if f["odno"] and f["odno"] not in known]
        buys = [f for f in missing if f["side"] == "buy"]
        sells = [f for f in missing if f["side"] == "sell"]
        item["detail"] = (f"모델에 없는 체결 매수 {sum(b['qty'] for b in buys)}주 / "
                          f"매도 {sum(s['qty'] for s in sells)}주")
        idle = [r for r in con.execute(
            "select id,tranche_num from tranches where ticker_id=? and status='IDLE' order by tranche_num", (tid,))]
        # 자동 보정 조건: 차이가 '모델에 없는 매수 체결' 로 정확히 설명되고, 빈 트렌치가 충분할 때만
        if (item["diff"] > 0 and buys and not sells
                and sum(b["qty"] for b in buys) == item["diff"] and len(idle) >= len(buys)):
            item["action"] = "편입" if apply else "편입 예정"
            if apply:
                bak = f"{db}.bak_{time.strftime('%Y%m%d_%H%M%S')}"
                shutil.copy(db, bak)
                cyc = con.execute("select current_cycle from tickers where id=?", (tid,)).fetchone()[0] or 1
                for f, (trid, tnum) in zip(buys, idle):
                    con.execute("update tranches set status='BOUGHT', qty=?, avg_price=?, buy_price=?, "
                                "buy_date=?, days_held=0, cycle_number=? where id=?",
                                (f["qty"], f["price"], f["price"], f["date"], cyc, trid))
                    con.execute("insert into trade_orders (ticker,tranche_id,side,order_type,price,qty,"
                                "order_date,status,kis_order_no,created_at) values (?,?,?,?,?,?,?,?,?,datetime('now'))",
                                (ticker, trid, "buy", "LOC", f["price"], f["qty"], f["date"], "filled", f["odno"]))
                    con.execute("insert into trades (tranche_id,ticker,tranche_num,cycle_number,side,order_type,"
                                "price,qty,amount,trade_date,created_at) values (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                                (trid, ticker, tnum, cyc, "buy", "LOC", f["price"], f["qty"],
                                 round(f["price"] * f["qty"], 2), f["date"]))
                    item["detail"] += f" · T{tnum} ← {f['qty']}주 @${f['price']} ({f['date']}, #{f['odno']})"
                con.execute("insert into app_logs (level,message,created_at) values (?,?,datetime('now'))",
                            ("INFO", f"[{ticker}] 수량 점검 보정: 계좌 실체결 {len(buys)}건 편입 "
                                     f"(모델 {model}주 → {real}주)"))
                con.commit()
                item["backup"] = bak
        found.append(item)
    con.close()
    return found


def check_infinite(client, ctac, acct) -> list[dict]:
    con = sqlite3.connect(f"file:{INFINITE_DB}?mode=ro", uri=True)
    out = []
    for ticker, qty in con.execute(
            "select p.ticker, s.qty from portfolios p join portfolio_states s on s.portfolio_id=p.id "
            "where p.is_active=1"):
        real = acct.get(ticker, 0)
        if int(qty or 0) != real:
            out.append({"strategy": "무한매수법", "ticker": ticker, "model": int(qty or 0),
                        "account": real, "diff": real - int(qty or 0), "action": "보고만",
                        "detail": "워커가 잔고에서 평단·수량을 다시 읽어 맞춘다 — 다음 실행 후에도 다르면 수동 확인"})
    con.close()
    return out


def check_vr() -> list[dict]:
    """VR(NH) 은 자체 수량감시(qty_audit)를 쓴다 — 상태만 읽어 보고."""
    out = []
    try:
        sys.path.insert(0, ".")
        from strategies.vr import models as VM
        from strategies.vr.worker import pending_target
        snaps = {s["gisu_id"]: s for s in VM.snapshots()}
        for g in VM.all_gisu():
            s = snaps.get(g["id"]) or {}
            acct = int(s.get("qty") or 0)
            model = int(g["model_qty"]) * int(g["mult"])
            accum = int(g.get("pending_accum") or 0) if pending_target(g) > 0 else 0
            now = acct - model - accum
            base = g.get("qty_offset")
            if base is not None and now != int(base):
                out.append({"strategy": f"VR {g['name']}", "ticker": g["ticker"], "model": model,
                            "account": acct, "diff": now - int(base), "action": "보고만",
                            "detail": f"체결 감시 기준 {base} → 현재 {now} (VR 화면에서 확인)"})
    except Exception as e:
        out.append({"strategy": "VR", "ticker": "-", "model": 0, "account": 0, "diff": 0,
                    "action": "확인실패", "detail": str(e)[:120]})
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    from strategies.ddsop.worker import get_shared_client
    client, ctac = get_shared_client()
    if not client.auth(ctac):
        print(json.dumps({"ok": False, "error": "KIS 인증 실패"}, ensure_ascii=False))
        return 2
    acct = account_qty(client, ctac)
    items = []
    for key, label, db in TRANCHE_DBS:
        items += check_tranche_strategy(key, label, db, client, ctac, acct, apply)
    items += check_infinite(client, ctac, acct)
    items += check_vr()

    result = {"ok": True, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "applied": apply,
              "account": acct, "mismatch": len(items), "items": items}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
