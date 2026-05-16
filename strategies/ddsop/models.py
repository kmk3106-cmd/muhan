# -*- coding: utf-8 -*-
"""
떨사오팔 v1 - DB 모델
"""
import enum
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, Integer, Float, String, Boolean, DateTime, Enum, Text,
    ForeignKey, inspect, text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TrancheStatus(enum.Enum):
    IDLE = "IDLE"
    BOUGHT = "BOUGHT"


class Ticker(Base):
    __tablename__ = "tickers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    total_usd: Mapped[float] = mapped_column(Float, nullable=False)
    num_tranches: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    x_pct: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    loss_cut_days: Mapped[int] = mapped_column(Integer, default=40)  # 손절 거래일
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    current_cycle: Mapped[int] = mapped_column(Integer, default=1)
    seed_reflect_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # ON: 추가입금분을 잔여트렌치에 반영
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tranches = relationship("Tranche", back_populates="ticker_rel", cascade="all, delete-orphan")
    # 성공리포트(CycleHistory)는 "영구 업적"으로 남겨야 하므로, 티커 삭제/정리 시 함께 삭제되지 않게 한다.
    cycles = relationship("CycleHistory", back_populates="ticker_rel")


class Tranche(Base):
    __tablename__ = "tranches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"), nullable=False)
    tranche_num: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=TrancheStatus.IDLE.value)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    buy_date: Mapped[str] = mapped_column(String(8), default="")
    buy_price: Mapped[float] = mapped_column(Float, default=0.0)
    days_held: Mapped[int] = mapped_column(Integer, default=0)
    amount_per_tranche: Mapped[float] = mapped_column(Float, default=0.0)
    cycle_number: Mapped[int] = mapped_column(Integer, default=1)

    ticker_rel = relationship("Ticker", back_populates="tranches")
    orders = relationship("TradeOrder", back_populates="tranche_rel", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="tranche_rel", cascade="all, delete-orphan")


class TradeOrder(Base):
    __tablename__ = "trade_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tranche_id: Mapped[int] = mapped_column(Integer, ForeignKey("tranches.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    order_date: Mapped[str] = mapped_column(String(8), default="")
    kis_order_no: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tranche_rel = relationship("Tranche", back_populates="orders")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tranche_id: Mapped[int] = mapped_column(Integer, ForeignKey("tranches.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    tranche_num: Mapped[int] = mapped_column(Integer, default=0)
    cycle_number: Mapped[int] = mapped_column(Integer, default=1)  # 몇 번 싸이클
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    trade_date: Mapped[str] = mapped_column(String(8), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tranche_rel = relationship("Tranche", back_populates="trades")


class CycleHistory(Base):
    __tablename__ = "cycle_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[str] = mapped_column(String(8), default="")
    end_date: Mapped[str] = mapped_column(String(8), default="")
    total_buy_amount: Mapped[float] = mapped_column(Float, default=0.0)
    total_sell_amount: Mapped[float] = mapped_column(Float, default=0.0)
    profit: Mapped[float] = mapped_column(Float, default=0.0)
    profit_pct: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ticker_rel = relationship("Ticker", back_populates="cycles")


class AppLog(Base):
    __tablename__ = "app_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SystemConfig(Base):
    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, default="")


def init_db(database_url: str | None = None):
    from .config import DATABASE_URL as DEFAULT_URL
    url = database_url or DEFAULT_URL
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    # loss_cut_days 컬럼 마이그레이션
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE tickers ADD COLUMN loss_cut_days INTEGER DEFAULT 40"
            ))
            conn.commit()
    except Exception:
        pass
    # trades.cycle_number 컬럼 마이그레이션
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE trades ADD COLUMN cycle_number INTEGER DEFAULT 1"
            ))
            conn.commit()
    except Exception:
        pass
    # tickers.seed_reflect_enabled 컬럼 마이그레이션
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE tickers ADD COLUMN seed_reflect_enabled INTEGER DEFAULT 0"
            ))
            conn.commit()
    except Exception:
        pass
    return engine
