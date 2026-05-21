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

echo "[2/7] Kill Switch 상태 영속화 + 폴더 교체"
# 폴더 교체와 무관한 영속 상태 디렉터리(배포 체인 중단에도 안 사라짐).
# 교체 직전 현재 live .kill_switch 유무를 STATE 에 스냅샷 → UI 토글도 영속.
STATE=/root/trading_suite_state
mkdir -p "$STATE"
for s in infinite ddsop; do
    if [ -f "$APP/strategies/$s/.kill_switch" ]; then
        touch "$STATE/$s.killed"
    elif [ -d "$APP/strategies/$s" ]; then
        rm -f "$STATE/$s.killed"
    fi
done
rm -rf "$BAK"
[ -d "$APP" ] && mv "$APP" "$BAK"
mv "$NEW" "$APP"

echo "[3/7] DB/예산/Kill Switch 복원"
if [ -d "$BAK" ]; then
    cp "$BAK/strategies/infinite/infinite_buy.db" "$APP/strategies/infinite/" 2>/dev/null || true
    cp "$BAK/strategies/ddsop/ddsop.db"           "$APP/strategies/ddsop/"    2>/dev/null || true
    cp "$BAK/core/_strategy_budget.json"          "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_equity.jsonl"                  "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_cashflow.json"                 "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_t_audit.jsonl"                 "$APP/core/"                2>/dev/null || true
else
    cp /root/infinite/infinite_buy.db "$APP/strategies/infinite/" 2>/dev/null || true
    cp /root/ddsop/ddsop.db           "$APP/strategies/ddsop/"    2>/dev/null || true
fi
# Kill Switch: 영속 상태(STATE) 기준으로 강제 — 정지 전략은 무슨 일이 있어도 정지 유지
for s in infinite ddsop; do
    if [ -f "$STATE/$s.killed" ]; then
        touch "$APP/strategies/$s/.kill_switch"
        echo "  [killswitch] $s = 정지 유지"
    else
        rm -f "$APP/strategies/$s/.kill_switch" 2>/dev/null || true
    fi
done

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
