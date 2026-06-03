# -*- coding: utf-8 -*-
"""전략별 KIS API 호출 시간대 레인 (P0 설계).

단일 KIS 계좌에서 전략들이 동시에 KIS API를 때리지 않도록 스케줄을
서로 다른 분/초 레인으로 분리한다. 여기서 정의하는 것은 APScheduler
트리거 설정값뿐 — 워커/kis_client의 호출 로직·API 메커니즘은 무관하다.

레인 (어느 분·초에도 호출 전략 ≤ 1):
- 무한매수법 (canonical, 기존 그대로): 워커 :run_m:00, sync 홀수분 :20초
- 떨사오팔        : 워커 :run_m+15분, sync 짝수분 :40초, pre_auth run_m-3
"""

# ---- 무한매수법 레인 (참조용; infinite/main.py는 이미 이 값으로 동작, 무수정) ----
INFINITE_WORKER_MIN_OFFSET = 0
INFINITE_PRE_AUTH_LEAD_MIN = 5      # 워커 run_m 의 5분 전
INFINITE_SYNC_MINUTE = "1-59/2"     # 홀수분
INFINITE_SYNC_SECOND = 20

# ---- 떨사오팔 레인 ----
DDSOP_WORKER_MIN_OFFSET = 15        # 무한 워커(:00) 와 15분 분리
DDSOP_PRE_AUTH_LEAD_MIN = 3         # 워커 run_m 의 3분 전 (무한 -5 와 분리)
DDSOP_SYNC_MINUTE = "0-58/2"        # 짝수분 (무한 홀수분과 분리)
DDSOP_SYNC_SECOND = 40              # 무한 :20초와 20초 이상 분리

# ---- 종사종팔 레인 ----
# 워커: 무한(:00)·떨사오팔(+15) 와 분리 → +25분 오프셋 (개장+30분 기준 run_m 대비)
# sync: 짝수분 :10초 (떨사오팔 짝수분 :40초와 30초 격차, 무한 홀수분 :20초와 비충돌)
# pre_auth: 워커 -4분 (무한 -5, 떨사오팔 -3 과 분리)
JONGSA_WORKER_MIN_OFFSET = 25       # 무한 :00, 떨사오팔 +15 와 분리
JONGSA_PRE_AUTH_LEAD_MIN = 4        # 워커 run_m 의 4분 전 (무한 -5, 떨사오팔 -3 과 분리)
JONGSA_SYNC_MINUTE = "0-58/2"       # 짝수분
JONGSA_SYNC_SECOND = 10             # 떨사오팔 :40초, 무한 :20초와 비충돌


def shift(hour: int, minute: int, delta_min: int) -> tuple[int, int]:
    """(hour, minute) 에 delta_min(±) 가감. 시 캐리/24h 모듈로 처리."""
    total = (hour * 60 + minute + delta_min) % (24 * 60)
    return total // 60, total % 60
