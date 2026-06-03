# -*- coding: utf-8 -*-
"""
종사종팔 v1 - DB 초기화
"""
import sys
from .models import init_db
from .config import DATABASE_URL

if __name__ == "__main__":
    engine = init_db(DATABASE_URL)
    print(f"DB 초기화 완료: {DATABASE_URL}")
