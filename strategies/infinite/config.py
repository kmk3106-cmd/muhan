# -*- coding: utf-8 -*-
"""
무한매수법 V2.2 - 전역 설정
환경변수 또는 기본값으로 앱 동작을 제어합니다.
"""
import os
from pathlib import Path

# ========== 경로 설정 ==========
# 이 config.py 파일이 있는 디렉터리 (프로젝트 루트)
BASE_DIR = Path(__file__).resolve().parent

# KIS API 설정 파일 경로 (yaml 사용 시, 대시보드 설정 사용 시에는 미사용)
# Windows: C:\Users\USER\KIS\config\kis_devlp.yaml
KIS_CONFIG_DIR = Path(os.environ.get("KIS_CONFIG_DIR", Path.home() / "KIS" / "config"))
KIS_DEVL_YAML = KIS_CONFIG_DIR / "kis_devlp.yaml"

# ========== DB 설정 ==========
# SQLite 기본 사용. Docker/클라우드 배포 시 환경변수로 /app/data/infinite_buy.db 등 지정
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{BASE_DIR / 'infinite_buy.db'}"
)

# ========== 스케줄러 설정 ==========
# 미국장 개시(9:30 ET) 후 30분 = 10:00 ET
# 서머타임(EDT, 3~11월): KST 23:00  /  동절기(EST, 11~3월): KST 00:00 (다음날)
# 동적 계산은 worker.py의 get_us_market_run_time()에서 처리
RUN_HOUR = int(os.environ.get("RUN_HOUR", 23))
RUN_MINUTE = int(os.environ.get("RUN_MINUTE", 0))

# ========== Kill Switch ==========
# 이 파일이 존재하면 워커 실행을 중지. 긴급정지용
# Docker 배포 시 /app/data/.kill_switch 로 볼륨 마운트
KILL_SWITCH_FILE = Path(os.environ.get("KILL_SWITCH_FILE", str(BASE_DIR / ".kill_switch")))

# ========== 거래 모드 ==========
# real: 실전투자, demo: 모의투자. 대시보드 설정이 우선됨
TRADING_MODE = os.environ.get("TRADING_MODE", "demo")

# ========== 연락처 ==========
# 한국투자 API 주문 시 필수. 대시보드 설정이 우선됨
CTAC_TLNO = os.environ.get("CTAC_TLNO", "01000000000")
