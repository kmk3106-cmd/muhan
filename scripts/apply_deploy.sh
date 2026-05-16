#!/bin/bash
# trading_suite 단일 배포 적용 (NCP 서버에서 실행).
# trading_suite_new -> trading_suite 교체, DB 2개+예산 보존, 단일 프로세스(8000) 기동,
# 레거시 /root/infinite·/root/ddsop 정지 및 포트 8001 폐기.
set -u
cd /root

APP=/root/trading_suite
NEW=/root/trading_suite_new
BAK=/root/trading_suite.bak
VENV=/root/trading_suite_venv

echo "[1/7] 기존 프로세스 정지 (suite + 레거시 infinite/ddsop)"
# 통합 프로세스
pkill -f "trading_suite/.*main.py" 2>/dev/null || true
pkill -f "trading_suite_venv/bin/python main.py" 2>/dev/null || true
# 레거시 단일 전략 프로세스 (운영전환 시 폐기)
systemctl is-active --quiet infinite_buy 2>/dev/null && systemctl stop infinite_buy 2>/dev/null || true
pkill -f "/root/infinite/.*main.py" 2>/dev/null || true
pkill -f "ddsop.*main.py" 2>/dev/null || true
sleep 2
fuser -k 8000/tcp 2>/dev/null || true
fuser -k 8001/tcp 2>/dev/null || true   # 8001 폐기
sleep 1

echo "[2/7] 폴더 교체"
rm -rf "$BAK"
[ -d "$APP" ] && mv "$APP" "$BAK"
mv "$NEW" "$APP"

echo "[3/7] DB/상태 복원"
if [ -d "$BAK" ]; then
    # 재배포: 직전 trading_suite 의 DB·예산 유지
    cp "$BAK/strategies/infinite/infinite_buy.db" "$APP/strategies/infinite/" 2>/dev/null || true
    cp "$BAK/strategies/ddsop/ddsop.db"           "$APP/strategies/ddsop/"    2>/dev/null || true
    cp "$BAK/core/_strategy_budget.json"          "$APP/core/"                2>/dev/null || true
else
    # 최초 배포: 레거시 단일 전략 DB 에서 이관 (실거래 상태 보존)
    cp /root/infinite/infinite_buy.db "$APP/strategies/infinite/" 2>/dev/null || true
    cp /root/ddsop/ddsop.db           "$APP/strategies/ddsop/"    2>/dev/null || true
fi

echo "[4/7] venv (고정 경로, 배포마다 유지)"
[ -d "$VENV" ] || python3 -m venv "$VENV"
source "$VENV/bin/activate"

echo "[5/7] 의존성 설치"
pip install -q --upgrade pip
pip install -q -r "$APP/requirements.txt"

echo "[6/7] 단일 프로세스 기동 (port 8000)"
cd "$APP"
nohup "$VENV/bin/python" main.py > app.log 2>&1 &
sleep 3

echo "[7/7] 헬스체크"
code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ || echo 000)
ic=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/infinite/dashboard || echo 000)
dc=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ddsop/dashboard || echo 000)
echo "  / -> $code  /infinite/dashboard -> $ic  /ddsop/dashboard -> $dc"
if [ "$code" = "200" ] && [ "$ic" = "200" ] && [ "$dc" = "200" ]; then
    echo "DONE - trading_suite 기동 OK (롤백본: $BAK)"
else
    echo "WARN - 헬스체크 실패. app.log 확인. 롤백: rm -rf $APP; mv $BAK $APP; cd $APP; nohup $VENV/bin/python main.py > app.log 2>&1 &"
fi
