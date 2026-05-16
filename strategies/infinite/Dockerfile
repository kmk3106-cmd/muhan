# 무한매수법 V2.2 - NCP/클라우드 배포용
FROM python:3.11-slim

WORKDIR /app

# 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 코드 복사
COPY . .

# DB/데이터 영구 저장용 디렉터리 (실행 시 -v 로 마운트)
RUN mkdir -p /app/data
ENV DATABASE_URL=sqlite:////app/data/infinite_buy.db
ENV KILL_SWITCH_FILE=/app/data/.kill_switch

EXPOSE 8000

# 0.0.0.0 바인딩으로 외부 접속 허용
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
