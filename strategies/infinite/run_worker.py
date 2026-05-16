# -*- coding: utf-8 -*-
"""
워커 수동 실행 (테스트용)
스케줄 대기 없이 즉시 1회 실행.
로컬에서 동작 확인할 때 사용.
"""
import logging
from .worker import run_worker_once
from .config import CTAC_TLNO

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

if __name__ == "__main__":
    result = run_worker_once(CTAC_TLNO)
    print("Result:", result)
