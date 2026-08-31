FROM python:3.11-slim

ARG APP_COMMIT=unknown
ENV APP_COMMIT=${APP_COMMIT}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD ["python", "scripts/healthcheck.py"]

CMD ["python", "-m", "src.main"]
