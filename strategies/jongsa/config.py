# -*- coding: utf-8 -*-
"""
종사종팔 v1 - 전역 설정
(떨사오팔 ddsop 패키지 복제. 매수만 종가 MOC 무조건으로 교체 — trading_logic 참조)
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 주의: DATABASE_URL 환경변수는 전역 공유라 운영에선 미설정(각 전략 BASE_DIR 기본값 사용).
# 종사종팔 전용 DB 파일로 분리 → ddsop.db 와 절대 공유하지 않음.
DATABASE_URL = os.getenv("JONGSA_DATABASE_URL", f"sqlite:///{BASE_DIR / 'jongsa.db'}")

# 스케줄러 실행 시각 (KST) - 미국장 개시 30분 후
RUN_HOUR = int(os.getenv("RUN_HOUR", "0"))
RUN_MINUTE = int(os.getenv("RUN_MINUTE", "0"))

TRADING_MODE = os.getenv("TRADING_MODE", "real")
CTAC_TLNO = os.getenv("CTAC_TLNO", "01000000000")

KIS_DEVL_YAML = os.getenv("KIS_DEVL_YAML", str(BASE_DIR / "kis_devlp.yaml"))

KILL_SWITCH_FILE = BASE_DIR / ".kill_switch"

SERVER_PORT = int(os.getenv("JONGSA_SERVER_PORT", "8002"))
