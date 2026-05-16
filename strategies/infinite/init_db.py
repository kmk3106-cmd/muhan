# -*- coding: utf-8 -*-
"""
DB 초기화 및 샘플 포트폴리오 등록
테이블 생성 + (선택) AAPL 샘플 포트폴리오 추가

사용: python init_db.py [--sample]
  --sample: 샘플 포트폴리오(AAPL, seed=4000, A=40) 등록
"""
import sys
from .config import DATABASE_URL
from .models import init_db, Portfolio, PortfolioState
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


def main():
    engine = create_engine(DATABASE_URL)
    init_db(DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        count = len(session.scalars(select(Portfolio).where(Portfolio.is_active == True)).all())
        if count == 0 and "--sample" in sys.argv:
            # 샘플 포트폴리오: AAPL, 4000달러, 40회 분할
            pf = Portfolio(ticker="AAPL", strategy_version="2.2", seed=4000, A=40, R=10.0, ovrs_excg_cd="NASD")
            session.add(pf)
            session.commit()
            state = PortfolioState(portfolio_id=pf.id)
            session.add(state)
            session.commit()
            print(f"샘플 포트폴리오 등록: {pf.ticker} (seed={pf.seed}, A={pf.A})")
        else:
            print(f"DB 초기화 완료. 활성 포트폴리오: {count}개")


if __name__ == "__main__":
    main()
