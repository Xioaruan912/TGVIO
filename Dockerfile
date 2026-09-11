FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg openssh-client ca-certificates build-essential gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG APP_COMMIT=unknown
ENV APP_COMMIT=${APP_COMMIT}

COPY src ./src
COPY scripts ./scripts

ENV PYTHONPATH=/app/src

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "scripts/healthcheck.py"]

CMD ["python", "-m", "tgvio.main"]

