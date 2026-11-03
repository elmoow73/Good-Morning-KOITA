# GoodMorningKOITA 클라우드 서버용 이미지
#   docker build -t goodmorningkoita .
#   docker run -d -p 8000:8000 -v koita-data:/data --env-file .env goodmorningkoita
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/data \
    OPEN_BROWSER=0 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY . .
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["python", "app.py"]
