@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8777"
set "URL=http://127.0.0.1:%PORT%"

echo.
echo   백테스트 UI   %URL%
echo   무한매수법 V2.2 / 떨사오팔 / 종사종팔4 / VR 5.0
echo.
echo   이 창을 닫으면 서버가 꺼집니다.
echo.

if not exist ".venv\Scripts\python.exe" (
  echo   [오류] .venv 를 찾을 수 없습니다: %cd%\.venv
  echo.
  pause
  exit /b 1
)

rem 서버가 뜰 시간을 준 뒤 브라우저를 연다.
rem cmd /c 중첩 따옴표는 깨지기 쉬워 powershell 로 분리 실행한다.
rem BT_NOBROWSER=1 이면 열지 않는다 (자동 테스트용).
if not "%BT_NOBROWSER%"=="1" (
  start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%URL%'"
)

.venv\Scripts\python.exe -m backtest.server --port %PORT%

echo.
echo   서버가 종료되었습니다.
if not "%BT_NOBROWSER%"=="1" pause
