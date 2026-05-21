@echo off
REM trading_suite 단일 배포 (무한매수법+떨사오팔 통합, 단일 프로세스 8000).
REM deploy_all.bat 대체. 실행 시 NCP 서버에 적용됨 — 운영 전환은 신중히.
cd /d "%~dp0"

set "PEM=C:\Users\USER\Documents\infinite\infinite-key.pem"
set "SRV=root@49.50.135.220"
if not exist "%PEM%" (
    echo [Error] PEM key not found: %PEM%
    pause
    exit /b 1
)

echo ============================================
echo   Deploy: trading_suite (single unit :8000)
echo ============================================

echo [1/4] Preparing...
set "T=%TEMP%\tsuite_deploy_%RANDOM%"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
REM 로컬 런타임 상태(JSONL/JSON)는 서버 측 .bak 보존본으로 복원되므로 업로드에서 제외
robocopy "%~dp0." "%T%" /E /XD .venv venv __pycache__ .git .idea .cursor .claude /XF *.db *.pem *.pyc *.zip _equity.jsonl _t_audit.jsonl _cashflow.json _strategy_budget.json /NFL /NDL /NJH /NJS /nc /ns /np >nul

echo [2/4] Uploading...
ssh -i "%PEM%" -o StrictHostKeyChecking=no -o ConnectTimeout=30 %SRV% "rm -rf /root/trading_suite_new; mkdir -p /root/trading_suite_new"
scp -i "%PEM%" -o StrictHostKeyChecking=no -o ConnectTimeout=30 -r "%T%\*" %SRV%:/root/trading_suite_new/

echo [3/4] Apply + Restart...
ssh -i "%PEM%" -o StrictHostKeyChecking=no -o ConnectTimeout=60 %SRV% "sed -i 's/\r$//' /root/trading_suite_new/scripts/apply_deploy.sh; bash /root/trading_suite_new/scripts/apply_deploy.sh"

echo [4/4] Cleanup...
rmdir /s /q "%T%" 2>nul

echo ============================================
echo   Done.  http://49.50.135.220:8000/
echo   탭: 무한매수법 / 떨사오팔  (포트 8001 폐기됨)
echo ============================================
pause
