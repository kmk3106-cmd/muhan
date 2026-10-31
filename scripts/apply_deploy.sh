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
for s in infinite ddsop jongsa vr; do
    if [ -f "$APP/strategies/$s/.kill_switch" ]; then
        touch "$STATE/$s.killed"
    elif [ -d "$APP/strategies/$s" ]; then
        rm -f "$STATE/$s.killed"
    fi
done
# ── 가드: 서버에만 있고 git 에 없는 코드 변경을 덮지 않는다 ─────────────────
# 2026-10 사고: 종사종팔 v4 가 커밋 없이 서버에서만 돌고 있었다. 이 스크립트는 폴더를
# 통째로 바꾸고 DB·상태파일만 되살리므로, 그대로 배포했으면 v4 가 조용히 사라졌다.
#
# 판정법: 배포할 때마다 $APP 에 .deploy_manifest(코드파일 md5)를 남긴다.
#   현재 $APP 파일이 '직전 배포 당시 해시'와 다르고, '새 패키지 내용'과도 다르면
#   → 배포 이후 서버에서 바뀌었고 git 에도 반영 안 된 것 = 덮으면 소실.
#   (서버에서 고친 뒤 커밋까지 했으면 새 패키지와 같아지므로 통과한다)
# 우회: FORCE_DEPLOY=1 bash scripts/apply_deploy.sh
_mf="$APP/.deploy_manifest"
if [ -d "$APP" ] && [ -f "$_mf" ] && [ "${FORCE_DEPLOY:-0}" != "1" ]; then
    _lost=$("$VENV/bin/python" - "$APP" "$NEW" "$_mf" <<'PYGUARD'
import hashlib, os, sys
app, new, mf = sys.argv[1], sys.argv[2], sys.argv[3]
EXT = (".py", ".html", ".sh", ".md", ".txt", ".yaml")
SKIPDIR = (".venv", "__pycache__", ".git")

def md5(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except Exception:
        return None

old = {}
for raw in open(mf, encoding="utf-8", errors="replace"):
    line = raw.strip()
    if not line or " " not in line:
        continue
    h, rel = line.split(" ", 1)
    old[rel] = h

lost = []
for root, dirs, files in os.walk(app):
    dirs[:] = [d for d in dirs if d not in SKIPDIR]
    for fn in files:
        if not fn.endswith(EXT):
            continue
        full = os.path.join(root, fn)
        rel = os.path.relpath(full, app).replace(os.sep, "/")
        cur = md5(full)
        if cur is None or cur == old.get(rel):
            continue
        if md5(os.path.join(new, rel)) == cur:
            continue
        lost.append(rel)

for rel in sorted(lost):
    print(rel)
PYGUARD
)
    if [ -n "$_lost" ]; then
        echo ""
        echo "[중단] 서버에만 있고 git 에 없는 변경 $(echo "$_lost" | grep -c .)건:"
        echo "$_lost" | sed 's/^/  /'
        echo ""
        echo "먼저 커밋·푸시한 뒤 다시 배포하거나, 의도한 폐기면"
        echo "  FORCE_DEPLOY=1 bash scripts/apply_deploy.sh"
        echo "로 실행하세요. 현재 운영 상태는 그대로 두고 종료합니다."
        exit 2
    fi
    echo "  가드 통과 — 서버 코드가 전부 git 에 있습니다"
elif [ "${FORCE_DEPLOY:-0}" = "1" ]; then
    echo "  [경고] FORCE_DEPLOY=1 — 가드를 건너뜁니다"
else
    echo "  가드 생략 (직전 배포 기록 없음 — 이번 배포부터 기록합니다)"
fi

rm -rf "$BAK"
[ -d "$APP" ] && mv "$APP" "$BAK"
mv "$NEW" "$APP"

# 이번에 배포한 코드 해시를 남긴다 (다음 배포의 가드 기준)
"$VENV/bin/python" - "$APP" <<'PYMANIFEST' > "$APP/.deploy_manifest" 2>/dev/null || true
import hashlib, os, sys
app = sys.argv[1]
EXT = (".py", ".html", ".sh", ".md", ".txt", ".yaml")
SKIPDIR = (".venv", "__pycache__", ".git")
for root, dirs, files in os.walk(app):
    dirs[:] = [d for d in dirs if d not in SKIPDIR]
    for fn in sorted(files):
        if not fn.endswith(EXT):
            continue
        full = os.path.join(root, fn)
        rel = os.path.relpath(full, app).replace(os.sep, "/")
        try:
            with open(full, "rb") as fh:
                print(hashlib.md5(fh.read()).hexdigest(), rel)
        except Exception:
            pass
PYMANIFEST

echo "[3/7] DB/예산/Kill Switch 복원"
# [2026-09-29 수정] DB 가 WAL 모드다. .db 만 복사하면 **마지막 체크포인트 이후 커밋이 통째로 유실**된다
# (실제 사고: 배포 직전 DB 수정 2건이 배포 후 사라짐 — WAL 크기가 수백KB~2MB 로 상시 존재).
# 프로세스는 [1/7] 에서 이미 정지됐으므로 여기서 체크포인트를 돌려 WAL 을 본체에 반영한 뒤 복사한다.
# 체크포인트가 실패해도 -wal/-shm 을 함께 복사해 두므로 데이터는 보존된다.
if [ -d "$BAK" ]; then
    for _db in "$BAK/strategies/infinite/infinite_buy.db" "$BAK/strategies/ddsop/ddsop.db" \
               "$BAK/strategies/jongsa/jongsa.db" "$BAK/strategies/vr/vr.db"; do
        [ -f "$_db" ] || continue
        "$VENV/bin/python" - "$_db" <<'PYCKPT' 2>/dev/null || echo "  [경고] 체크포인트 실패: $_db (WAL 동반 복사로 보존)"
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=20)
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.close()
PYCKPT
    done
    # .db 와 함께 -wal/-shm 도 복사 (체크포인트가 실패했을 때의 안전망)
    for _pair in "strategies/infinite/infinite_buy.db:strategies/infinite" \
                 "strategies/ddsop/ddsop.db:strategies/ddsop" \
                 "strategies/jongsa/jongsa.db:strategies/jongsa" \
                 "strategies/vr/vr.db:strategies/vr"; do
        _src="${_pair%%:*}"; _dst="${_pair##*:}"
        cp "$BAK/$_src"      "$APP/$_dst/" 2>/dev/null || true
        cp "$BAK/$_src-wal"  "$APP/$_dst/" 2>/dev/null || true
        cp "$BAK/$_src-shm"  "$APP/$_dst/" 2>/dev/null || true
    done
    cp "$BAK/core/_strategy_budget.json"          "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_equity.jsonl"                  "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_cashflow.json"                 "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_t_audit.jsonl"                 "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_holdings.json"                 "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_compound.json"                 "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_toss.json"                     "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_toss_series.jsonl"              "$APP/core/"                2>/dev/null || true
    cp "$BAK/core/_equity_backfill.jsonl"         "$APP/core/"                2>/dev/null || true
else
    cp /root/infinite/infinite_buy.db "$APP/strategies/infinite/" 2>/dev/null || true
    cp /root/ddsop/ddsop.db           "$APP/strategies/ddsop/"    2>/dev/null || true
fi
# Kill Switch: 영속 상태(STATE) 기준으로 강제 — 정지 전략은 무슨 일이 있어도 정지 유지
for s in infinite ddsop jongsa vr; do
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

# 기동 시 4개 서브앱 lifespan(스케줄러 등록·초기 동기화)이 먼저 돌아
# 포트 바인딩까지 1분 안팎 걸린다. 바인딩될 때까지 대기(최대 180초).
echo "[7/7] 헬스체크 (기동 대기)"
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ || echo 000)
    [ "$code" = "200" ] && { echo "  기동 확인 (약 $((i*6))초)"; break; }
    sleep 6
done
ic=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/infinite/dashboard || echo 000)
dc=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ddsop/dashboard || echo 000)
jc=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/jongsa/dashboard || echo 000)
vc=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/vr/ || echo 000)
echo "  / -> $code  /infinite/dashboard -> $ic  /ddsop/dashboard -> $dc  /jongsa/dashboard -> $jc  /vr -> $vc"
if [ "$code" = "200" ] && [ "$ic" = "200" ] && [ "$dc" = "200" ] && [ "$jc" = "200" ]; then
    echo "DONE - trading_suite 기동 OK (롤백본: $BAK)"
else
    echo "WARN - 헬스체크 실패. app.log 확인. 롤백: rm -rf $APP; mv $BAK $APP; cd $APP; nohup $VENV/bin/python main.py > app.log 2>&1 &"
fi
