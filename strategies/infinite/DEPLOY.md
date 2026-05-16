# 무한매수법 V2.2 - 서버 배포 가이드

서버 `49.50.135.220`에 프로젝트를 배포하는 방법입니다.

---

## ⚡ 빠른 배포 (코드 변경 후 매번 실행)

**방법 1: 더블클릭** (가장 간단)

- `infinite_buy_v22` 폴더 안의 **`배포.bat`** 파일을 더블클릭

**방법 2: PowerShell에서 실행**

```powershell
cd C:\Users\USER\infinite_buy_v22
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

**방법 3: CMD에서 실행**

```cmd
cd C:\Users\USER\infinite_buy_v22
powershell -ExecutionPolicy Bypass -File deploy.ps1
```

> 실행 정책 오류 시 반드시 `-ExecutionPolicy Bypass` 사용

---

## 자동 배포 (압축 없이 파일만 전송)

위 명령어가 실행하는 `deploy.ps1` 스크립트가 하는 일:

1. `venv`, `__pycache__`, `*.db`를 제외한 파일만 준비
2. `scp -r`로 서버 `/root/infinite`에 직접 전송 (압축 없음)
3. 서버에서 기존 DB 유지 후 앱 재시작

**실행 전 확인**:
- `infinite-key.pem` 위치: `C:\Users\USER\Documents\infinite\`
- 서버: `49.50.135.220`

---

## ⚠️ [2/6] Uploading에서 멈출 때 (Railway 등 PaaS)

**원인**: 업로드 용량이 크거나(.venv, .git 포함), 네트워크 타임아웃.

**해결**:

1. **`.railwayignore` 사용**  
   프로젝트 루트에 `.railwayignore`가 있으면 Railway 등이 `.venv`, `.git`, `__pycache__` 등을 제외합니다. (이미 추가해 두었습니다.)

2. **GitHub 연동으로 배포**  
   `railway up` 대신 **Git 저장소 연동**으로 배포하면, 서버가 Git에서만 받아와서 용량·타임아웃 문제가 줄어듭니다.  
   Railway 대시보드 → 프로젝트 → Settings → Connect GitHub repo.

3. **로컬에서 제외 확인**  
   - `.venv`, `venv` 폴더가 배포 대상에 포함되지 않도록 하세요.  
   - `deploy.ps1`은 이미 `.venv`, `.git`, `.cursor`, `data`를 제외하도록 수정되어 있습니다.

4. **다른 네트워크에서 시도**  
   회사/학교 방화벽이 업로드를 막는 경우, 다른 네트워크(예: 휴대폰 핫스팟)에서 한 번 시도해 보세요.

---

## Git 기반 자동 배포 (선택)

서버에 Git 저장소를 두고 `git pull`로 배포하려면:

### 1. 서버에 저장소 클론 (최초 1회)

```bash
cd /root
git clone https://github.com/당신계정/infinite_buy_v22.git infinite
cd infinite
chmod +x scripts/deploy_server.sh
# venv, pip install, systemd 설정 등 (기존 4~5단계)
```

### 2. 배포 시 (로컬에서 push 후)

```bash
ssh -i infinite-key.pem root@49.50.135.220 "cd /root/infinite && git pull && ./scripts/deploy_server.sh"
```

### 3. 또는 로컬 배포 스크립트에 Git 옵션 추가

`deploy.ps1` 대신 `git push` 후 서버에서 `git pull && deploy_server.sh` 실행하도록 구성 가능.

---

## 전제 조건

- **SSH 접속**: `ssh -i infinite-key.pem root@49.50.135.220`
- **로컬 프로젝트**: `C:\Users\USER\infinite_buy_v22`
- **infinite-key.pem** 위치: `C:\Users\USER\Documents\infinite\infinite-key.pem`
- **서버 프로젝트 경로**: `/root/infinite`

---

## 1단계: 로컬에서 프로젝트 압축 (Windows)

PowerShell에서:

```powershell
cd C:\Users\USER
Compress-Archive -Path infinite_buy_v22 -DestinationPath infinite_buy_v22.zip -Force
```

또는 WSL/Git Bash가 있다면:

```bash
cd /mnt/c/Users/USER
tar -czvf infinite_buy_v22.tar.gz infinite_buy_v22 --exclude='infinite_buy_v22/venv' --exclude='infinite_buy_v22/__pycache__' --exclude='infinite_buy_v22/*.db'
```

> `venv`, `__pycache__`, `*.db`는 제외해도 됩니다 (서버에서 새로 생성).

---

## 2단계: 서버에 파일 업로드

PowerShell:

```powershell
cd C:\Users\USER
scp -i "C:\Users\USER\Documents\infinite\infinite-key.pem" infinite_buy_v22.zip root@49.50.135.220:/root/
```

또는 infinite 폴더에서 실행 시:

```powershell
cd "C:\Users\USER\Documents\infinite"
scp -i infinite-key.pem "C:\Users\USER\infinite_buy_v22.zip" root@49.50.135.220:/root/
```

---

## 3단계: 서버 SSH 접속

```powershell
cd "C:\Users\USER\Documents\infinite"
ssh -i infinite-key.pem root@49.50.135.220
```

---

## 4단계: 서버에서 압축 해제 및 환경 설정

### zip 사용 시

```bash
cd /root
apt-get update && apt-get install -y unzip python3 python3-pip python3-venv
unzip -o infinite_buy_v22.zip
mv infinite_buy_v22 infinite
cd infinite
```

### tar.gz 사용 시

```bash
cd /root
tar -xzvf infinite_buy_v22.tar.gz
mv infinite_buy_v22 infinite
cd infinite
```

### Python 가상환경 및 패키지 설치

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5단계: 실행 (선택)

### A. 수동 실행 (테스트용)

```bash
cd /root/infinite
source venv/bin/activate
python main.py
```

Ctrl+C로 종료. 정상 동작하면 다음 단계로.

### B. 백그라운드 실행 (nohup)

```bash
cd /root/infinite
source venv/bin/activate
nohup python main.py > app.log 2>&1 &
```

### C. systemd 서비스 (재부팅 후 자동 시작, 권장)

```bash
nano /etc/systemd/system/infinite_buy.service
```

아래 내용 입력 후 저장:

```ini
[Unit]
Description=무한매수법 V2.2 API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/infinite
Environment="PATH=/root/infinite/venv/bin"
ExecStart=/root/infinite/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

적용 및 실행:

```bash
systemctl daemon-reload
systemctl enable infinite_buy
systemctl start infinite_buy
systemctl status infinite_buy
```

---

## 6단계: 방화벽 확인

8000 포트가 열려 있어야 합니다.

```bash
# ufw 사용 시
ufw allow 8000
ufw reload

# iptables 사용 시 (서버에 따라 다름)
# ACG/보안그룹에서 8000 인바운드 허용 확인
```

---

## 7단계: 대시보드 접속

브라우저에서:

```
http://49.50.135.220:8000
```

또는:

```
http://49.50.135.220:8000/dashboard
```

**설정** 버튼 → 계좌정보, 앱키, 연락처 입력 후 저장.

---

## 유용한 명령어

| 작업 | 명령어 |
|------|--------|
| 로그 확인 | `tail -f /root/infinite/app.log` |
| systemd 로그 | `journalctl -u infinite_buy -f` |
| 프로세스 확인 | `ps aux \| grep main.py` |
| 서비스 재시작 | `systemctl restart infinite_buy` |
| 서비스 중지 | `systemctl stop infinite_buy` |

---

## 환경변수 (선택)

`/root/infinite/.env` 또는 systemd 서비스에 추가:

| 변수 | 설명 | 기본값 |
|------|------|--------|
| TRADING_MODE | real / demo | demo |
| RUN_HOUR | 실행 시각(시) | 5 |
| RUN_MINUTE | 실행 시각(분) | 55 |
| CTAC_TLNO | 연락처 | 01000000000 |

---

## 트러블슈팅

| 증상 | 확인 |
|------|------|
| 8000 접속 안 됨 | 방화벽/ACG에서 8000 허용 |
| ModuleNotFoundError | `pip install -r requirements.txt` 재실행 |
| KIS 인증 실패 | 대시보드 설정에서 앱키/시크릿 재입력 |
| DB 오류 | `/root/infinite` 디렉터리 쓰기 권한 확인 |
