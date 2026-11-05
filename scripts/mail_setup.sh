#!/bin/bash
# 일일 점검 메일 발송 설정 — Gmail 앱 비밀번호를 받아 서버에 저장한다.
#
# 앱 비밀번호는 구글 계정 비밀번호가 아니다. 2단계 인증을 켠 뒤
#   https://myaccount.google.com/apppasswords
# 에서 발급하는 16자리 전용 비밀번호이고, 메일 발송 외에는 쓸 수 없다.
#
# 저장 위치는 앱 폴더 밖(/root/trading_suite_state/mail.env)이라 배포해도 안 지워진다.
set -u

ENVF=/root/trading_suite_state/mail.env
VENV=/root/trading_suite_venv/bin/python

mkdir -p /root/trading_suite_state

echo "=== 일일 점검 메일 설정 ==="
if [ -f "$ENVF" ]; then
  echo "현재 설정:"
  sed -E 's/^(MAIL_APP_PASSWORD=).*/\1****************/' "$ENVF" | sed 's/^/  /'
  cp -p "$ENVF" "$ENVF.bak.$(date +%Y%m%d_%H%M%S)"
  echo "  (기존 파일 백업함)"
  echo
fi

# 주소는 인자로 받는다 (비밀 아님). 그러면 물어보는 칸이 '앱 비밀번호' 하나뿐이라
# 입력이 밀려서 비밀번호가 주소 칸에 들어가는 사고가 안 난다. (2026-11-05 실제로 발생)
#   사용법: mail_setup.sh <보내는주소> [받는주소]
MFROM=${1:-}
MTO=${2:-$MFROM}
if [ -z "$MFROM" ]; then
  read -r -p "보내는 Gmail 주소 (예: kmk3106@gmail.com): " MFROM
  [ -z "${MFROM:-}" ] && { echo "중단: 주소가 비었습니다."; exit 1; }
  MTO=$MFROM
fi
case "$MFROM" in
  *@*.*) ;;
  *) echo "중단: '$MFROM' 은 메일 주소가 아닙니다. 앱 비밀번호를 여기 넣지 마세요."; exit 1 ;;
esac
echo "보내는/받는 주소: $MFROM -> $MTO"
echo

echo "이제 앱 비밀번호 하나만 입력하면 됩니다. 쳐도 화면에 안 보이는 게 정상입니다."
read -r -s -p "앱 비밀번호 16자리: " MPW
echo
MPW=$(echo "$MPW" | tr -d ' ')
if [ ${#MPW} -ne 16 ]; then
  echo "중단: 앱 비밀번호는 공백 빼고 16자리입니다 (입력 ${#MPW}자리)."
  exit 1
fi
case "$MPW" in
  *@*) echo "중단: 메일 주소가 들어온 것 같습니다. 앱 비밀번호를 입력하세요."; exit 1 ;;
esac

umask 077
cat > "$ENVF" <<EOF
MAIL_FROM=$MFROM
MAIL_APP_PASSWORD=$MPW
MAIL_TO=$MTO
EOF
chmod 600 "$ENVF"
echo "저장 완료: $ENVF (권한 600)"
echo

echo "=== 발송 테스트 ==="
cd /root/trading_suite || exit 1
$VENV - <<'PY'
import sys
sys.path.insert(0, "/root/trading_suite")
from core.daily_review import build_report, render_html, send_mail
rep = build_report()
s = rep["session"]
n = len(rep["suspects"])
ok, detail = send_mail(
    "[자동매매] 메일 설정 테스트 — %s-%s-%s 일일점검" % (s[:4], s[4:6], s[6:]),
    render_html(rep))
print("  결과:", "성공" if ok else "실패", "-", detail)
print("  점검 필요 %d건" % n)
for x in rep["suspects"]:
    print("   -", x)
if not ok:
    sys.exit(1)
PY
rc=$?
echo
if [ $rc -eq 0 ]; then
  echo "메일함을 확인하세요. 이후 매일 07:02 KST 에 자동 발송됩니다."
  echo "앱 재시작은 필요 없습니다 (발송 때마다 파일을 다시 읽습니다)."
else
  echo "실패했습니다. 앱 비밀번호를 다시 확인하고 이 스크립트를 재실행하세요."
fi
exit $rc
