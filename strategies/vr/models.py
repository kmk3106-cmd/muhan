# -*- coding: utf-8 -*-
"""VR 상태 저장 (sqlite3 — 자체 완결 모듈).

gisu           : 기수별 설정+현재 주기 상태 (모델 수치 기준)
weekly_history : 주차별 V/밴드/평가금/Pool — 라오어식 그래프·주차표 재현용
reserved_orders: 예약주문 원장 (NH 예약접수번호 매핑)
fills          : 체결 반영 이력 (dailyTransaction 동기화, 모델 반영 dedup)
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from .config import DATABASE_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gisu (
  id TEXT PRIMARY KEY,             -- 'vr0' | 'vr5'
  name TEXT NOT NULL,              -- 표시명
  acct_no TEXT NOT NULL,           -- NH 계좌번호(11)
  ticker TEXT NOT NULL DEFAULT 'TQQQ',
  mult INTEGER NOT NULL DEFAULT 1,         -- 배수 (주문수량 = 모델단위×배수)
  unit INTEGER NOT NULL,                   -- 모델 단위 수량 (0기 4, 5기 2)
  buy_limit_pct REAL NOT NULL,             -- 매수한도 % (0기 25, 5기 10)
  sell_steps INTEGER NOT NULL,             -- 매도 사다리 단수
  g REAL NOT NULL,                          -- G (기수별, 26주마다 +1 — 수동 관리)
  cashflow REAL NOT NULL DEFAULT 0,        -- 주기당 현금흐름 (적립 +, 인출 −; V·Pool 계산 반영용)
  week_no INTEGER NOT NULL,
  cyc_start TEXT NOT NULL, cyc_end TEXT NOT NULL,   -- YYYYMMDD
  v REAL NOT NULL, band_lo REAL NOT NULL, band_hi REAL NOT NULL,
  model_qty INTEGER NOT NULL,              -- 모델 잔여개수 (주기 시작 기준 → 체결 반영 갱신)
  pool_start REAL NOT NULL,                -- 주기 시작 Pool (모델)
  pool_now REAL NOT NULL,                  -- 현재 Pool (모델, 체결 반영)
  auto_submit INTEGER NOT NULL DEFAULT 0,  -- 1=토요일 자동 산출·제출 (검증 후 ON 권장)
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS weekly_history (
  gisu_id TEXT NOT NULL, week_no INTEGER NOT NULL,
  v REAL, band_lo REAL, band_hi REAL,
  eval_amt REAL,                            -- 주차 마감 평가금 E
  pool_start REAL, pool_end REAL, traded_amt REAL,
  PRIMARY KEY (gisu_id, week_no)
);
CREATE TABLE IF NOT EXISTS reserved_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gisu_id TEXT NOT NULL, week_no INTEGER NOT NULL,
  side TEXT NOT NULL,                       -- buy | sell
  price REAL NOT NULL, qty_acct INTEGER NOT NULL,
  start_dt TEXT, end_dt TEXT,
  nh_order_dt TEXT, nh_order_no TEXT,       -- 예약주문일자·예약접수번호
  status TEXT NOT NULL DEFAULT 'submitted', -- submitted | cancelled | done | failed
  raw TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS acct_snapshot (
  gisu_id TEXT PRIMARY KEY,
  qty INTEGER, buy_usd REAL, close REAL, eval_usd REAL,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gisu_id TEXT NOT NULL, week_no INTEGER,
  side TEXT NOT NULL, price REAL NOT NULL, qty_acct INTEGER NOT NULL,
  fill_dt TEXT, dedup_key TEXT UNIQUE,      -- 원천행 식별자 (재동기화 중복 방지)
  created_at TEXT
);
"""


_initialized = False


@contextmanager
def db():
    global _initialized
    con = sqlite3.connect(str(DATABASE_PATH))
    con.row_factory = sqlite3.Row
    try:
        if not _initialized:
            con.executescript(_SCHEMA)
            for _mig in (  # 기존 DB 마이그레이션 (컬럼 없으면 추가)
                "ALTER TABLE gisu ADD COLUMN auto_submit INTEGER NOT NULL DEFAULT 0",
                # 계좌 현금 — 통화별로 따로 담고 환산은 조회 시점 환율로 (fx 도 함께 보관)
                "ALTER TABLE acct_snapshot ADD COLUMN cash_usd REAL DEFAULT 0",
                "ALTER TABLE acct_snapshot ADD COLUMN cash_krw REAL DEFAULT 0",
                "ALTER TABLE acct_snapshot ADD COLUMN fx REAL DEFAULT 0",
                "ALTER TABLE acct_snapshot ADD COLUMN cash_order REAL DEFAULT 0",
                # Pool 은 현금만이 아니라 RP·원화자산·타종목까지 포함한다.
                # NH PLUG 는 해외주식만 조회돼 이들이 안 보이므로 수동 입력분을 둔다.
                "ALTER TABLE gisu ADD COLUMN ext_assets REAL NOT NULL DEFAULT 0",
            ):
                try:
                    con.execute(_mig)
                except sqlite3.OperationalError:
                    pass
            _seed(con)
            con.commit()
            _initialized = True
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:  # lazy-init 트리거
        pass


def _seed(con: sqlite3.Connection):
    """최초 1회 시드 — 사용자 확정 상태 (2026-08-20 기준, 현 주기는 사용자가 이미 예약 완료).

    시스템은 '다음 주기'(0기 258주차 / 5기 49주차)부터 담당한다.
    """
    if con.execute("SELECT COUNT(*) FROM gisu").fetchone()[0] > 0:
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con.executemany(
        "INSERT INTO gisu (id,name,acct_no,ticker,mult,unit,buy_limit_pct,sell_steps,g,cashflow,"
        "week_no,cyc_start,cyc_end,v,band_lo,band_hi,model_qty,pool_start,pool_now,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # VR 0기 거치식 ×1배수 — 256주차 (8/17~8/28, 사용자 기제출)
            ("vr0", "VR 0기 거치식", "20101889977", "TQQQ", 1, 4, 25.0, 11, 16.0, 0.0,
             256, "20260817", "20260828", 30905.34, 26269.54, 35541.14, 430, 8185.59, 8185.59, now),
            # VR 5기 인출식 ×6배수 — 47주차 (8/10~8/21, 사용자 기제출) · 인출 $75/주기
            ("vr5", "VR 5기 인출식", "20101986635", "TQQQ", 6, 2, 10.0, 15, 41.0, -75.0,
             47, "20260810", "20260821", 15015.07, 12762.81, 17267.33, 216, 10028.16, 10028.16, now),
        ],
    )
    con.executemany(
        "INSERT OR IGNORE INTO weekly_history (gisu_id,week_no,v,band_lo,band_hi,eval_amt,pool_start,pool_end,traded_amt) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            # 팬딩 캡쳐에서 확보한 과거 주차 (그래프 시작점)
            ("vr0", 254, 30018.60, None, None, 33019.70, None, 8185.59, None),
            ("vr0", 256, 30905.34, 26269.54, 35541.14, None, 8185.59, None, None),
            ("vr5", 45, 14738.46, None, None, 16085.52, 11198.50, 10103.16, -1095.34),
            ("vr5", 47, 15015.07, 12762.81, 17267.33, None, 10028.16, None, None),
        ],
    )


def get_gisu(gid: str) -> dict | None:
    with db() as con:
        r = con.execute("SELECT * FROM gisu WHERE id=?", (gid,)).fetchone()
        return dict(r) if r else None


def all_gisu() -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM gisu ORDER BY id")]


def update_gisu(gid: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [gid]
    with db() as con:
        con.execute(f"UPDATE gisu SET {cols}, updated_at=datetime('now','localtime') WHERE id=?", vals)


def upsert_weekly(gid: str, week_no: int, **fields):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO weekly_history (gisu_id,week_no) VALUES (?,?)", (gid, week_no))
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            con.execute(
                f"UPDATE weekly_history SET {cols} WHERE gisu_id=? AND week_no=?",
                list(fields.values()) + [gid, week_no])


def weekly_rows(gid: str) -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM weekly_history WHERE gisu_id=? ORDER BY week_no", (gid,))]


def add_reserved(gid: str, week_no: int, side: str, price: float, qty_acct: int,
                 start_dt: str, end_dt: str, nh_order_dt: str, nh_order_no: str,
                 status: str, raw: dict | None):
    with db() as con:
        con.execute(
            "INSERT INTO reserved_orders (gisu_id,week_no,side,price,qty_acct,start_dt,end_dt,"
            "nh_order_dt,nh_order_no,status,raw,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
            (gid, week_no, side, price, qty_acct, start_dt, end_dt,
             nh_order_dt, nh_order_no, status, json.dumps(raw or {}, ensure_ascii=False)))


def reserved_rows(gid: str, week_no: int | None = None) -> list[dict]:
    with db() as con:
        q = "SELECT id,gisu_id,week_no,side,price,qty_acct,start_dt,end_dt,nh_order_dt,nh_order_no,status,created_at FROM reserved_orders WHERE gisu_id=?"
        args: list = [gid]
        if week_no is not None:
            q += " AND week_no=?"
            args.append(week_no)
        return [dict(r) for r in con.execute(q + " ORDER BY id", args)]


def upsert_snapshot(gid: str, qty: int, buy_usd: float, close: float, eval_usd: float,
                    cash_usd: float = 0.0, cash_krw: float = 0.0, fx: float = 0.0,
                    cash_order: float = 0.0):
    with db() as con:
        con.execute(
            "INSERT INTO acct_snapshot (gisu_id,qty,buy_usd,close,eval_usd,"
            "cash_usd,cash_krw,fx,cash_order,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime')) "
            "ON CONFLICT(gisu_id) DO UPDATE SET qty=excluded.qty, buy_usd=excluded.buy_usd, "
            "close=excluded.close, eval_usd=excluded.eval_usd, cash_usd=excluded.cash_usd, "
            "cash_krw=excluded.cash_krw, fx=excluded.fx, cash_order=excluded.cash_order, "
            "updated_at=excluded.updated_at",
            (gid, qty, buy_usd, close, eval_usd, cash_usd, cash_krw, fx, cash_order))


def snapshots() -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT s.*, g.name, g.acct_no, g.ticker, g.mult FROM acct_snapshot s "
            "JOIN gisu g ON g.id = s.gisu_id ORDER BY s.gisu_id")]


def add_fill(gid: str, week_no: int, side: str, price: float, qty_acct: int,
             fill_dt: str, dedup_key: str) -> bool:
    """중복이면 False."""
    with db() as con:
        try:
            con.execute(
                "INSERT INTO fills (gisu_id,week_no,side,price,qty_acct,fill_dt,dedup_key,created_at) "
                "VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))",
                (gid, week_no, side, price, qty_acct, fill_dt, dedup_key))
            return True
        except sqlite3.IntegrityError:
            return False


def fills_rows(gid: str, week_no: int | None = None) -> list[dict]:
    with db() as con:
        q = "SELECT * FROM fills WHERE gisu_id=?"
        args: list = [gid]
        if week_no is not None:
            q += " AND week_no=?"
            args.append(week_no)
        return [dict(r) for r in con.execute(q + " ORDER BY id", args)]
