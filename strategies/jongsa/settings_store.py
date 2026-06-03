# -*- coding: utf-8 -*-
"""
종사종팔 v1 - 설정 저장/조회 (DB 기반)
"""
from typing import Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL
from .models import SystemConfig, init_db

SETTINGS_KEYS = [
    "trading_mode", "my_app", "my_sec", "paper_app", "paper_sec",
    "my_acct_stock", "my_paper_stock", "my_prod", "ctac_tlno", "my_agent",
    "my_htsid",
]
MASK_KEYS = {"my_sec", "paper_sec"}


def _get_session():
    init_db(DATABASE_URL)
    engine = create_engine(DATABASE_URL)
    return sessionmaker(bind=engine)()


def get_setting(session: Session, key: str) -> Optional[str]:
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
    return row.value if row else None


def set_setting(session: Session, key: str, value: str):
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == key))
    if row:
        row.value = value
    else:
        session.add(SystemConfig(key=key, value=value))
    session.commit()


def get_kis_settings(database_url: str = DATABASE_URL) -> Optional[dict]:
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
    engine = create_engine(database_url)
    with Session(engine) as session:
        for k in SETTINGS_KEYS:
            if k in data and data[k] is not None and str(data[k]).strip():
                set_setting(session, k, str(data[k]).strip())


def save_account_summary(acct: dict, database_url: str = DATABASE_URL):
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
            "updated_at": datetime.now().isoformat(),
        })
        set_setting(session, "acct_summary", val)


def get_account_summary(database_url: str = DATABASE_URL) -> Optional[dict]:
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
