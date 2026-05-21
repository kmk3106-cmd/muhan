# -*- coding: utf-8 -*-
"""[ONE-TIME] ddsop TECL/SPXL 최근 매도된 싸이클 후행 보정.

상황: 초기화 시 current_cycle=1로 리셋되었으나 cycle_history는 영구보존이라
옛 (ticker_id, cycle_number=1) 기록과 충돌 → _check_and_record_cycle이
dedup early-return 하여 신규 싸이클이 history에 미기록.

조치:
  1) 최근 trades.cycle_number=1을 (history 최대+1)로 재라벨 (TECL: 4, SPXL: 3)
  2) 트렌치.cycle_number를 다음 싸이클(+2)로 진척
  3) cycle_history 행 삽입(매수/매도 합·실현손익·수익률)
  4) tickers.current_cycle 갱신
모두 단일 트랜잭션. 코드 fix(상위 커밋)와 함께 적용.
"""
import sqlite3
import sys

DB = "/root/trading_suite/strategies/ddsop/ddsop.db"

def patch_ticker(con, ticker_name: str):
    cur = con.cursor()
    row = cur.execute(
        "SELECT id, current_cycle FROM tickers WHERE ticker=?", (ticker_name,)
    ).fetchone()
    if not row:
        print(f"[{ticker_name}] not found"); return
    ticker_id, cur_cy = row
    max_cy = cur.execute(
        "SELECT COALESCE(MAX(cycle_number),0) FROM cycle_history WHERE ticker_id=?",
        (ticker_id,)
    ).fetchone()[0]
    target = max_cy + 1
    next_cy = target + 1
    # 미라벨 trades 수
    pre = cur.execute(
        "SELECT COUNT(*) FROM trades WHERE ticker=? AND cycle_number=1", (ticker_name,)
    ).fetchone()[0]
    if pre == 0:
        print(f"[{ticker_name}] no cycle=1 trades to relabel (max_cy={max_cy})"); return
    # 이미 cycle=target인 history가 있는지(있으면 스킵)
    dup = cur.execute(
        "SELECT id FROM cycle_history WHERE ticker_id=? AND cycle_number=?",
        (ticker_id, target)
    ).fetchone()
    if dup:
        print(f"[{ticker_name}] history already has cycle={target} (id={dup[0]}) — skip"); return

    # 1) trades 재라벨 → target
    cur.execute(
        "UPDATE trades SET cycle_number=? WHERE ticker=? AND cycle_number=1",
        (target, ticker_name)
    )
    # 2) 트렌치 cycle_number → next_cy (다음 싸이클 진척)
    cur.execute(
        "UPDATE tranches SET cycle_number=? WHERE ticker_id=?",
        (next_cy, ticker_id)
    )

    # 3) 합계·실현손익 계산(매수 매칭: tranche_id·date<=sell)
    trades = cur.execute(
        "SELECT id,tranche_id,side,price,qty,amount,trade_date FROM trades "
        "WHERE ticker=? AND cycle_number=? ORDER BY id",
        (ticker_name, target)
    ).fetchall()
    total_buy_all = sum(t[5] for t in trades if t[2] == "buy")
    total_sell = 0.0
    realized = 0.0
    total_buy_for_sold = 0.0
    for tid, tranche_id, side, price, qty, amount, td in trades:
        if side != "sell":
            continue
        total_sell += amount
        mb = cur.execute(
            "SELECT price FROM trades WHERE tranche_id=? AND side='buy' AND trade_date<=? "
            "ORDER BY trade_date DESC LIMIT 1",
            (tranche_id, td)
        ).fetchone()
        if mb:
            cost = mb[0] * qty
            total_buy_for_sold += cost
            realized += amount - cost
    profit = round(realized, 2)
    pct = round((profit / total_buy_for_sold * 100) if total_buy_for_sold > 0 else 0, 2)

    dates = [t[6] for t in trades if t[6]]
    start_d = min(dates); end_d = max(t[6] for t in trades if t[2] == "sell" and t[6]) or start_d

    # 4) cycle_history 삽입
    cur.execute(
        "INSERT INTO cycle_history(ticker_id,ticker,cycle_number,start_date,end_date,"
        "total_buy_amount,total_sell_amount,profit,profit_pct,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))",
        (ticker_id, ticker_name, target, start_d, end_d,
         round(total_buy_all, 2), round(total_sell, 2), profit, pct)
    )
    # 5) tickers.current_cycle = next_cy
    cur.execute("UPDATE tickers SET current_cycle=? WHERE id=?", (next_cy, ticker_id))
    print(f"[{ticker_name}] relabeled {pre} trades cy=1→{target}, tranches→{next_cy}, "
          f"history+1 cy={target} buy={round(total_buy_all,2)} sell={round(total_sell,2)} "
          f"profit={profit} pct={pct}%, current_cycle={cur_cy}→{next_cy}")


def main():
    con = sqlite3.connect(DB)
    try:
        con.execute("BEGIN")
        patch_ticker(con, "TECL")
        patch_ticker(con, "SPXL")
        con.commit()
        print("COMMITTED")
        # 사후 검증
        cur = con.cursor()
        print("--cycle_history(ALL)--")
        for r in cur.execute(
            "SELECT id,ticker_id,ticker,cycle_number,start_date,end_date,profit,profit_pct "
            "FROM cycle_history ORDER BY id"
        ).fetchall():
            print(" ", r)
        print("--tickers--")
        for r in cur.execute("SELECT id,ticker,current_cycle FROM tickers").fetchall():
            print(" ", r)
    except Exception as e:
        con.rollback()
        print("ROLLBACK:", e); sys.exit(1)
    finally:
        con.close()

if __name__ == "__main__":
    main()
