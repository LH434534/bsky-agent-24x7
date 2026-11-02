FROM python:3.11-slim

# Sem compilação, sem Gradle, sem systemd: só o loop 24/7 nativo (main.py run).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Sao_Paulo

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY main.py immortal.py gh_runner.py ./
COPY scripts/ ./scripts/

# Estado sobrevive ao recreate do container
VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8080/health || python3 -c \
      "from core.beacon import read, age, alive; import sys; sys.exit(0 if alive(read('worker')) and age(read('worker'))<300 else 1)"

CMD ["python3", "main.py", "run"]
