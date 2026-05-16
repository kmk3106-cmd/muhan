# -*- coding: utf-8 -*-
"""LOC매도취소 원인 확인: AppLog + TradeOrder 검색"""
import sys
sys.path.insert(0, ".")
from .config import DATABASE_URL
from sqlalchemy import create_engine, text

def main():
    engine = create_engine(DATABASE_URL)
    print("=" * 60)
    print("1) AppLog 검색 (미체결 주문 취소 / 종목 OFF / 30728977)")
    print("=" * 60)
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT id, level, message, created_at
            FROM app_logs
            WHERE message LIKE '%미체결 주문 취소%'
               OR message LIKE '%종목 OFF%'
               OR message LIKE '%30728977%'
            ORDER BY id DESC
            LIMIT 30
        """))
        rows = r.fetchall()
    if not rows:
        print("(해당 로그 없음)")
    else:
        for r in rows:
            print(f"  [{r[1]}] {r[3]} | {r[2][:120]}")

    print()
    print("=" * 60)
    print("2) TradeOrder kis_order_no='30728977' 검색")
    print("=" * 60)
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT id, ticker, side, order_type, price, qty, status, order_date, kis_order_no, created_at
            FROM trade_orders
            WHERE kis_order_no = '30728977' OR kis_order_no LIKE '%30728977%'
            ORDER BY id DESC
        """))
        rows = r.fetchall()
    if not rows:
        print("(해당 주문번호 레코드 없음)")
    else:
        for r in rows:
            print(f"  id={r[0]} ticker={r[1]} {r[2]} {r[3]} price={r[4]} qty={r[5]} status={r[6]} order_date={r[7]} kis_order_no={r[8]} created={r[9]}")
    print()

if __name__ == "__main__":
    main()
