# -*- coding: utf-8 -*-
"""[ONE-TIME] 무한매수법 CycleHistory.end_date 후행 보정.

증상: worker._check_cycle_end가 end_date=today(종료처리 실행일)로 기록 →
같은 날 다음 싸이클 첫 매수가 있는 경우 /api/cycles/{id}/trades 의
date-range 쿼리(start_date<=trade_date<=end_date)가 다음 싸이클 매수를 포함.

조치: 각 CycleHistory 행의 end_date를 (start_date, end_date) 범위 내
'마지막 매도' trade_date로 재설정. 없으면 그대로 둠. 단일 트랜잭션.
worker.py 수정과 함께 적용(코드 fix는 이후 새 싸이클 종료에만 적용되므로 과거분은 이 스크립트로).
"""
import sqlite3

DB = "/root/trading_suite/strategies/infinite/infinite_buy.db"

def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT id,portfolio_id,cycle_number,start_date,end_date FROM cycle_history "
        "ORDER BY portfolio_id, cycle_number"
    ).fetchall()
    print(f"총 {len(rows)}개 CycleHistory 확인")
    fixed = 0
    try:
        con.execute("BEGIN")
        for cid, pid, cn, sd, ed in rows:
            last_sell = cur.execute(
                "SELECT MAX(trade_date) FROM trades "
                "WHERE portfolio_id=? AND side='sell' "
                "AND trade_date>=? AND trade_date<=?",
                (pid, sd, ed)
            ).fetchone()[0]
            if not last_sell:
                print(f"  pf={pid} C{cn} ({sd}~{ed}) — sell 없음, 변경 안 함")
                continue
            if last_sell == ed:
                continue  # 이미 정확
            # 같은 날 다음 싸이클 매수가 실제로 있는지 확인
            future_buy = cur.execute(
                "SELECT COUNT(*) FROM trades "
                "WHERE portfolio_id=? AND side='buy' "
                "AND trade_date>? AND trade_date<=?",
                (pid, last_sell, ed)
            ).fetchone()[0]
            print(f"  pf={pid} C{cn}: end_date {ed} → {last_sell} (후속매수 {future_buy}건 제외)")
            cur.execute(
                "UPDATE cycle_history SET end_date=? WHERE id=?",
                (last_sell, cid)
            )
            fixed += 1
        con.commit()
        print(f"COMMITTED — {fixed}개 행 보정")
        # 사후 검증: TQQQ C2 트레이드 수
        print("--사후 검증 (포트폴리오별 cycle_history)--")
        for pid, ticker, cn, sd, ed in cur.execute(
            "SELECT ch.portfolio_id,p.ticker,ch.cycle_number,ch.start_date,ch.end_date "
            "FROM cycle_history ch JOIN portfolios p ON p.id=ch.portfolio_id "
            "ORDER BY ch.portfolio_id, ch.cycle_number"
        ).fetchall():
            nt = cur.execute(
                "SELECT COUNT(*) FROM trades WHERE portfolio_id=? "
                "AND trade_date>=? AND trade_date<=?", (pid, sd, ed)
            ).fetchone()[0]
            print(f"  {ticker} C{cn}: {sd}~{ed}  trades={nt}건")
    except Exception as e:
        con.rollback()
        print("ROLLBACK:", e)
    finally:
        con.close()

if __name__ == "__main__":
    main()
