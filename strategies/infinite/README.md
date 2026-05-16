# 무한매수법 V2.2 - 한국투자증권 API 자동매매

웹 기반 서버에서 24/7 자동 운용 + 한국투자증권 OpenAPI(REST) 조합으로 무한매수법 V2.2를 구현합니다.

## 구조

- **API 서버 (FastAPI)**: 포트폴리오 관리, 대시보드, Kill Switch, 강제 동기화
- **워커/스케줄러**: 매일 미국장 마감 전 1회 주문 생성·전송
- **DB (SQLite)**: 상태 저장, 봇이 꺼져도 이어서 운용

## 설치

```bash
cd infinite_buy_v22
pip install -r requirements.txt
```

## 설정

1. **KIS API 설정**

   KIS Developers(https://apiportal.koreainvestment.com)에서 앱키/시크릿 발급 후:

   ```bash
   mkdir -p %USERPROFILE%\KIS\config    # Windows
   copy kis_devlp_sample.yaml %USERPROFILE%\KIS\config\kis_devlp.yaml
   ```

   `kis_devlp.yaml`을 열어 앱키, 시크릿, 계좌번호를 입력하세요.

2. **환경 변수 (선택)**

   | 변수 | 설명 | 기본값 |
   |------|------|--------|
   | TRADING_MODE | real(실전) / demo(모의) | demo |
   | CTAC_TLNO | 연락처 (주문 시 필수) | 01000000000 |
   | RUN_HOUR | 실행 시각 (시) | 5 |
   | RUN_MINUTE | 실행 시각 (분) | 55 |
   | DATABASE_URL | DB 경로 | sqlite:///infinite_buy.db |

## 실행

```bash
python main.py
```

- API: http://localhost:8000
- 대시보드: http://localhost:8000/dashboard
- API 문서: http://localhost:8000/docs

## API 예시

```bash
# 포트폴리오 등록
curl -X POST http://localhost:8000/api/portfolios \
  -H "Content-Type: application/json" \
  -d '{"ticker":"AAPL","seed":4000,"A":40}'

# 강제 동기화 (워커 1회 실행)
curl -X POST http://localhost:8000/api/sync

# Kill Switch 활성화 (긴급정지)
curl -X POST "http://localhost:8000/api/kill_switch?activate=true"
```

## 스케줄

- **추천**: 미국장 마감 5분 전 (KST 05:55 또는 06:55, 서머타임에 따라 변경)
- 기본: 매일 05:55 KST

## 주의사항

1. **모의투자**: LOC/MOC는 모의투자에서 제한될 수 있습니다. 실전에서 테스트 전 모의투자로 흐름 확인을 권장합니다.
2. **서머타임**: 미국 서머타임 시 장마감 KST가 바뀝니다. `RUN_HOUR`를 시즌에 맞게 조정하세요.
3. **체결 지연**: 미국주식 체결/잔고 반영이 지연될 수 있어, 다음날 동기화 루틴이 중요합니다.

## 라이선스

참고용 샘플입니다. 실제 투자에 활용할 시 발생하는 손해에 대해 제작자는 책임지지 않습니다.
