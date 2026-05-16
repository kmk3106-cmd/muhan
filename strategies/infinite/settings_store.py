# -*- coding: utf-8 -*-
"""
설정 저장/조회 (DB 기반)
대시보드에서 입력한 계좌/앱키를 SystemConfig 테이블에 저장.
KIS 클라이언트는 DB 설정을 우선 사용 (yaml 폴백)
"""
from typing import Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL
from .models import SystemConfig, init_db

# DB에 저장하는 설정 키 목록
SETTINGS_KEYS = [
    "trading_mode", "my_app", "my_sec", "paper_app", "paper_sec",
    "my_acct_stock", "my_paper_stock", "my_prod", "ctac_tlno", "my_agent",
    "my_htsid",
]
# 조회 시 마스킹할 키 (앱시크릿 노출 방지)
MASK_KEYS = {"my_sec", "paper_sec"}


def _get_session():
    """DB 세션 생성 (init_db 먼저 호출)"""
    init_db(DATABASE_URL)
    engine = create_engine(DATABASE_URL)
    return sessionmaker(bind=engine)()


def get_setting(session: Session, key: str) -> Optional[str]:
    """단일 설정값 조회"""
    stmt = select(SystemConfig).where(SystemConfig.key == key)
    row = session.scalar(stmt)
    return row.value if row else None


def set_setting(session: Session, key: str, value: str):
    """단일 설정값 저장 (기존 있으면 업데이트)"""
    stmt = select(SystemConfig).where(SystemConfig.key == key)
    row = session.scalar(stmt)
    if row:
        row.value = value
    else:
        session.add(SystemConfig(key=key, value=value))
    session.commit()


def get_kis_settings(database_url: str = DATABASE_URL) -> Optional[dict]:
    """
    DB에서 KIS 설정 로드
    없으면 None 반환 (워커에서 yaml 폴백)
    반환 형식: kis_client에서 사용하는 cfg 딕셔너리
    """
    engine = create_engine(database_url)
    with Session(engine) as session:
        rows = session.scalars(select(SystemConfig)).all()
        if not rows:
            return None
        d = {r.key: r.value for r in rows}
        return {
            "my_app": d.get("my_app", ""),
            "my_sec": d.get("my_sec", ""),
            "paper_app": d.get("paper_app", ""),
            "paper_sec": d.get("paper_sec", ""),
            "my_acct_stock": d.get("my_acct_stock", ""),
            "my_paper_stock": d.get("my_paper_stock", ""),
            "my_prod": d.get("my_prod", "01"),
            "my_agent": d.get("my_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
            "prod": "https://openapi.koreainvestment.com:9443",
            "vps": "https://openapivts.koreainvestment.com:29443",
            "ops": "ws://ops.koreainvestment.com:21000",
            "vops": "ws://ops.koreainvestment.com:31000",
            "trading_mode": d.get("trading_mode", "demo"),
            "ctac_tlno": d.get("ctac_tlno", "01000000000"),
            "my_htsid": d.get("my_htsid") or "kmk3106",
        }


def get_settings_for_display(database_url: str = DATABASE_URL) -> dict:
    """
    대시보드용 설정 조회
    my_sec, paper_sec는 ******** 로 마스킹
    """
    engine = create_engine(database_url)
    with Session(engine) as session:
        rows = session.scalars(select(SystemConfig)).all()
        d = {r.key: r.value for r in rows}
    result = {}
    for k in SETTINGS_KEYS:
        v = d.get(k, "")
        if k in MASK_KEYS and v:
            v = "********" if len(v) <= 8 else v[:4] + "****" + v[-2:]
        result[k] = v
    return result


def save_settings(data: dict, database_url: str = DATABASE_URL):
    """
    대시보드에서 저장한 설정을 DB에 반영
    빈 값/None은 업데이트하지 않음 (기존 유지)
    """
    engine = create_engine(database_url)
    with Session(engine) as session:
        for k in SETTINGS_KEYS:
            if k in data and data[k] is not None and str(data[k]).strip():
                set_setting(session, k, str(data[k]).strip())


def save_account_summary(acct: dict, database_url: str = DATABASE_URL):
    """동기화 시 계좌 정보 저장 (대시보드 표시용)"""
    import json
    from datetime import datetime
    engine = create_engine(database_url)
    with Session(engine) as session:
        val = json.dumps({
            "stock_evlu": acct.get("stock_evlu", 0),
            "buy_amt": acct.get("buy_amt", 0),
            "cash": acct.get("cash", 0),
            "tot_asst_amt": acct.get("tot_asst_amt", 0),
            "exrt": acct.get("exrt", 0),
            "pnl": acct.get("pnl", 0),
            "pnl_rt": acct.get("pnl_rt", 0),
            "tot_pfls": acct.get("tot_pfls", 0),
            "updated_at": datetime.now().isoformat(),
        })
        set_setting(session, "acct_summary", val)


def get_account_summary(database_url: str = DATABASE_URL) -> Optional[dict]:
    """저장된 계좌총액/가용금 조회"""
    import json
    engine = create_engine(database_url)
    with Session(engine) as session:
        v = get_setting(session, "acct_summary")
        if not v:
            return None
        try:
            return json.loads(v)
        except Exception:
            return None
