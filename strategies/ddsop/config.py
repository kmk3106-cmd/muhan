# -*- coding: utf-8 -*-
"""
떨사오팔 v1 - 전역 설정
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'ddsop.db'}")

# 스케줄러 실행 시각 (KST) - 미국장 개시 30분 후
RUN_HOUR = int(os.getenv("RUN_HOUR", "0"))
RUN_MINUTE = int(os.getenv("RUN_MINUTE", "0"))

TRADING_MODE = os.getenv("TRADING_MODE", "real")
CTAC_TLNO = os.getenv("CTAC_TLNO", "01000000000")

KIS_DEVL_YAML = os.getenv("KIS_DEVL_YAML", str(BASE_DIR / "kis_devlp.yaml"))

KILL_SWITCH_FILE = BASE_DIR / ".kill_switch"

SERVER_PORT = int(os.getenv("SERVER_PORT", "8001"))
