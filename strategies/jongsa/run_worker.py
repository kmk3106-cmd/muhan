# -*- coding: utf-8 -*-
"""
종사종팔 v1 - 워커 수동 실행 (테스트용)
"""
import logging
from .worker import run_worker_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

if __name__ == "__main__":
    result = run_worker_once()
    print(f"\n결과: {result}")
