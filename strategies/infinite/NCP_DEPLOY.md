# 네이버 클라우드 플랫폼(NCP) 배포 가이드

무한매수법 V2.2를 NCP 서버에 배포하여 24시간 실행 + 대시보드 접속하는 방법입니다.

---

## 1. NCP 서버 준비

### 1-1. 서버 생성

1. [네이버 클라우드 플랫폼](https://www.ncloud.com) 로그인
2. **Server** → **Server** → **Server 생성**
3. **Ubuntu 22.04** 선택
4. 사양: **Micro** (무료) 또는 **Small** 이상
5. **공인 IP** 연결 (필수)
6. **ACG(접근제어)** 생성 후 포트 허용:
   - SSH: **22**
   - 대시보드: **8000**

### 1-2. ACG(방화벽) 설정

| 방향 | 프로토콜 | 접근소스 | 허용포트 |
|------|----------|----------|----------|
| Inbound | TCP | 0.0.0.0/0 (전체) | 22 (SSH) |
| Inbound | TCP | 0.0.0.0/0 (전체) | 8000 (대시보드) |

---

## 2. 배포 방법

### 방법 A: 직접 업로드 (권장)

#### Step 1. 프로젝트 압축

로컬 PC에서:
```powershell
cd C:\Users\USER
tar -czvf infinite_buy_v22.tar.gz infinite_buy_v22
# 또는 zip 사용
```

#### Step 2. NCP 서버에 업로드

```powershell
scp infinite_buy_v22.tar.gz root@공인IP주소:/root/
```

#### Step 3. NCP 서버 SSH 접속

```powershell
ssh root@공인IP주소
```

#### Step 4. 압축 해제 및 실행

```bash
cd /root
tar -xzvf infinite_buy_v22.tar.gz
cd infinite_buy_v22

# Python 환경 설치
apt-get update && apt-get install -y python3 python3-pip python3-venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 실행 (백그라운드)
nohup python main.py > app.log 2>&1 &
```

#### Step 5. 대시보드 접속

브라우저에서: **http://공인IP주소:8000/dashboard**

---

### 방법 B: Docker 사용

#### Step 1. NCP 서버에 Docker 설치

```bash
apt-get update && apt-get install -y docker.io
```

#### Step 2. 프로젝트 업로드 후 빌드/실행

```bash
cd /root/infinite_buy_v22
docker build -t infinite_buy .
docker run -d --name infinite_buy -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  infinite_buy
```

---

### 방법 C: systemd 서비스 (재부팅 후 자동 시작)

`deploy_ncp.sh` 실행 후:

```bash
# 서비스 파일 수정 (WorkingDirectory, User 등 실제 경로에 맞게)
sudo nano /etc/systemd/system/infinite_buy.service

# 적용
sudo systemctl daemon-reload
sudo systemctl enable infinite_buy
sudo systemctl start infinite_buy
sudo systemctl status infinite_buy
```

---

## 3. 배포 후 할 일

1. **대시보드 접속**: http://공인IP:8000/dashboard
2. **설정** 버튼 → 계좌정보, 앱키, 연락처 입력 후 저장
3. **포트폴리오 등록**: API `/api/portfolios` POST 또는 추후 UI에서
4. **스케줄 확인**: 매일 05:55 KST에 자동 실행 (config에서 변경 가능)

---

## 4. 보안 권장사항

- **HTTPS**: 도메인 연결 시 Let's Encrypt + Nginx 리버스 프록시 권장
- **방화벽**: 8000 포트를 특정 IP만 허용하도록 ACG 제한
- **설정**: 대시보드에서 입력한 앱키/시크릿은 DB에 저장됨 (SQLite 파일 권한 확인)

---

## 5. 트러블슈팅

| 증상 | 확인 사항 |
|------|-----------|
| 대시보드 접속 안 됨 | ACG에서 8000 포트 허용 여부 |
| API 인증 실패 | 대시보드 설정에서 앱키/시크릿 재입력 |
| 프로세스 종료됨 | `nohup` 사용 또는 systemd 서비스 등록 |
| DB 오류 | `data/` 디렉터리 쓰기 권한 확인 |

---

## 6. 요금 참고

- **Micro 서버**: 무료 (제한 있음)
- **Small 이상**: 시간당 과금
- **공인 IP**: 월 약 4,000원 수준

자세한 요금은 [NCP 요금 안내](https://www.ncloud.com/product/charge) 참고.
