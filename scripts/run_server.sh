#!/bin/bash
# trading_suite 수동 재시작 (NCP 서버, /root/trading_suite 에서). apply_deploy 없이 재기동용.
set -e
APP=/root/trading_suite
VENV=/root/trading_suite_venv
cd "$APP"

echo "=== trading_suite 재시작 (port 8000) ==="
pkill -f "trading_suite_venv/bin/python main.py" 2>/dev/null || true
pkill -f "trading_suite/.*main.py" 2>/dev/null || true
fuser -k 8000/tcp 2>/dev/null || true
sleep 2

[ -d "$VENV" ] || python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install -q -r requirements.txt

nohup "$VENV/bin/python" main.py > app.log 2>&1 &
sleep 3
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ | grep -q 200; then
    echo "OK - http://$(hostname -I | awk '{print $1}'):8000/  (탭: 무한매수법 / 떨사오팔)"
else
    echo "Started (app.log 확인)"
fi
