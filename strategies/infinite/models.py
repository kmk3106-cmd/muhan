# -*- coding: utf-8 -*-
"""
무한매수법 V2.2 - DB 모델 (SQLAlchemy ORM)
포트폴리오, 상태, 주문, 로그, 시스템설정 테이블 정의
"""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column, DateTime, Enum as SQLEnum, Float, Integer, String, Text, Boolean,
    ForeignKey, create_engine, text
)
from sqlalchemy.orm import DeclarativeBase, relationship, Mapped, mapped_column


# ========== ORM 베이스 ==========
class Base(DeclarativeBase):
    """모든 모델의 공통 베이스 클래스"""
    pass


# ========== 운영 모드 열거 ==========
class ModeEnum(str, Enum):
    """
    무한매수법 운영 모드
    - NORMAL: 일반 모드 (전반전 T<20 / 후반전 T>=20)
    - QUARTER: 쿼터손절 모드 (39 < T <= 40 진입 후)
    """
    NORMAL = "NORMAL"
    QUARTER = "QUARTER"


# ========== 포트폴리오 ==========
class Portfolio(Base):
    """
    포트폴리오 설정
    종목(티커)별 투자 규칙: Seed, 분할회차(A), 목표수익률(R) 등
    """
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)   # 종목코드: AAPL, SOXL 등
    strategy_version: Mapped[str] = mapped_column(String(10), default="2.2")  # 무한매수법 버전: 2.2, 3.0 등
    seed: Mapped[float] = mapped_column(Float, nullable=False)        # P0, 총 투자금(달러)
    A: Mapped[int] = mapped_column(Integer, default=40)               # 전체 회차 = 분할 일수
    R: Mapped[float] = mapped_column(Float, default=10.0)             # 목표 수익률 (%)
    fee_rate: Mapped[float] = mapped_column(Float, default=0.0)       # 수수료율
    ovrs_excg_cd: Mapped[str] = mapped_column(String(10), default="NASD")  # 거래소: NASD/NYSE/AMEX
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)    # 활성 여부 (삭제 시 False)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # 진행 ON/OFF (True=매일 실행)
    initial_buy_done: Mapped[bool] = mapped_column(Boolean, default=False)  # 최초 시장가 매수 완료 여부
    current_cycle: Mapped[int] = mapped_column(Integer, default=1)  # 현재 싸이클 번호
    cycle_start_date: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # 현재 싸이클 시작일
    cycle_start_trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 현재 싸이클 시작 Trade.id (같은 날 재진입 구분)
    initial_holdings_cost: Mapped[float] = mapped_column(Float, default=0.0)  # 기존 보유분 매입금액 (T 계산 시 cum_buy에 가산)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def B(self) -> float:
        """1회 투자금 = Seed / A"""
        return self.seed / self.A if self.A else 0

    # 관계: 상태/주문/로그
    states: Mapped[list["PortfolioState"]] = relationship(back_populates="portfolio", cascade="all, delete-orphan")
    orders: Mapped[list["Order"]] = relationship(back_populates="portfolio", cascade="all, delete-orphan")
    logs: Mapped[list["AppLog"]] = relationship(back_populates="portfolio", cascade="all, delete-orphan")


# ========== 포트폴리오 상태 ==========
class PortfolioState(Base):
    """
    포트폴리오별 현재 상태 (API 동기화 스냅샷)
    워커가 매일 잔고/체결을 조회해 갱신
    """
    __tablename__ = "portfolio_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), nullable=False)

    # 계좌 상태 (API 조회 결과)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)      # 평균매입가
    qty: Mapped[int] = mapped_column(Integer, default=0)              # 보유수량
    cash: Mapped[float] = mapped_column(Float, default=0.0)           # 가용현금(USD)

    # 체결 기반 (무한매수법 T 계산용)
    cum_buy_amount: Mapped[float] = mapped_column(Float, default=0.0)  # 누적 매수금액
    cum_sell_amount: Mapped[float] = mapped_column(Float, default=0.0) # 누적 매도금액

    # 진행값 (T, ☆% 는 (cum_buy - cum_sell) / B 기준으로 계산)
    T: Mapped[float] = mapped_column(Float, default=0.0)              # 현재 회차 = (cum_buy - cum_sell) / B
    star_pct: Mapped[float] = mapped_column(Float, default=0.0)       # ☆% = LOC 기준 퍼센트

    # 쿼터손절 모드용
    mode: Mapped[str] = mapped_column(String(20), default=ModeEnum.NORMAL.value)
    quarter_step: Mapped[int] = mapped_column(Integer, default=0)     # 쿼터 1~10 회차
    quarter_base_cash: Mapped[float] = mapped_column(Float, default=0.0)  # 쿼터용 1회 매수금
    last_moc_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # MOC 체결가

    # 중복 주문 방지
    last_run_date: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)   # YYYYMMDD
    last_orders_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 주문 세트 해시

    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    portfolio: Mapped["Portfolio"] = relationship(back_populates="states")


# ========== 주문 기록 ==========
class Order(Base):
    """
    주문 기록 (API 제출 결과)
    매일 생성된 주문의 성공/실패, 거부사유 등 저장
    """
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), nullable=False)
    order_date: Mapped[str] = mapped_column(String(8), nullable=False)  # YYYYMMDD

    # 주문 내용
    side: Mapped[str] = mapped_column(String(10), nullable=False)     # buy / sell
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)  # LOC / MOC / LIMIT
    ord_dvsn: Mapped[str] = mapped_column(String(10), nullable=False)  # 00/33/34 (한국투자 API 코드)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Float, default=0.0)         # 매수 시 금액

    # API 응답
    odno: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)   # 주문번호
    ord_dt: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    ord_gno_brno: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/success/fail
    msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)    # 거부사유 등

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    portfolio: Mapped["Portfolio"] = relationship(back_populates="orders")


# ========== 체결 거래내역 ==========
class Trade(Base):
    """실제 체결된 거래내역 (성공한 주문만 기록)"""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), nullable=False)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)       # 실제 체결가
    order_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # LOC 등 주문가(체결가와 다를 수 있음)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Float, default=0.0)          # 체결금액 = 실제 체결가 × 수량
    odno: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    buy_seq: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # 매수 회차: "최초", "2", "3", ... / 매도는 NULL
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    portfolio: Mapped["Portfolio"] = relationship()


# ========== 앱 로그 ==========
class AppLog(Base):
    """
    앱 로그 (거부사유, 오류, 재시도 등)
    대시보드 로그 화면에서 조회
    """
    __tablename__ = "app_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[Optional[int]] = mapped_column(ForeignKey("portfolios.id"), nullable=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO")    # INFO / WARNING / ERROR
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    portfolio: Mapped[Optional["Portfolio"]] = relationship(back_populates="logs")


# ========== 싸이클 이력 ==========
class CycleHistory(Base):
    """
    싸이클별 수익 기록
    1싸이클 = 최초매수 ~ 전량매도 (qty→0)
    """
    __tablename__ = "cycle_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), nullable=False)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[str] = mapped_column(String(8), nullable=False)
    end_date: Mapped[str] = mapped_column(String(8), nullable=False)
    total_buy_amount: Mapped[float] = mapped_column(Float, default=0.0)
    total_sell_amount: Mapped[float] = mapped_column(Float, default=0.0)
    profit: Mapped[float] = mapped_column(Float, default=0.0)
    profit_pct: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    portfolio: Mapped["Portfolio"] = relationship()


# ========== 시스템 설정 ==========
class SystemConfig(Base):
    """
    시스템 설정 (key-value)
    대시보드에서 입력한 계좌/앱키 등 저장
    """
    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========== DB 초기화 ==========
def init_db(database_url: str):
    """
    DB 엔진 생성 및 테이블 생성
    앱 시작 시 호출
    """
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(engine)
    # 기존 DB에 strategy_version 컬럼 추가 (마이그레이션)
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE portfolios ADD COLUMN strategy_version VARCHAR(10) DEFAULT '2.2'"
            ))
            conn.commit()
            # 기존 행에 기본값 적용
            conn.execute(text(
                "UPDATE portfolios SET strategy_version = '2.2' WHERE strategy_version IS NULL"
            ))
            conn.commit()
    except Exception:
        pass  # 컬럼 이미 존재 등 무시
    # trading_enabled 컬럼 추가 (마이그레이션)
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE portfolios ADD COLUMN trading_enabled BOOLEAN DEFAULT 1"
            ))
            conn.commit()
    except Exception:
        pass
    # initial_buy_done 컬럼 추가 (최초 매수 완료 여부)
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE portfolios ADD COLUMN initial_buy_done BOOLEAN DEFAULT 0"
            ))
            conn.commit()
    except Exception:
        pass
    # current_cycle, cycle_start_date 컬럼 추가
    for col, dtype, default in [
        ("current_cycle", "INTEGER", "1"),
        ("cycle_start_date", "VARCHAR(8)", "NULL"),
        ("cycle_start_trade_id", "INTEGER", "NULL"),
    ]:
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    f"ALTER TABLE portfolios ADD COLUMN {col} {dtype} DEFAULT {default}"
                ))
                conn.commit()
        except Exception:
            pass
    # cum_sell_amount 컬럼 추가 (T = (cum_buy - cum_sell) / B)
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE portfolio_states ADD COLUMN cum_sell_amount FLOAT DEFAULT 0.0"
            ))
            conn.commit()
    except Exception:
        pass
    # trades.order_price 컬럼 추가 (LOC 등 주문가 ≠ 체결가 표시용)
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE trades ADD COLUMN order_price FLOAT"
            ))
            conn.commit()
    except Exception:
        pass
    # trades.buy_seq 컬럼 추가 (매수 회차: "최초", "2", "3", ...)
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE trades ADD COLUMN buy_seq VARCHAR(10)"
            ))
            conn.commit()
    except Exception:
        pass
    # portfolios.initial_holdings_cost (기존 보유분 매입금액)
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE portfolios ADD COLUMN initial_holdings_cost FLOAT DEFAULT 0.0"
            ))
            conn.commit()
    except Exception:
        pass
    return engine
