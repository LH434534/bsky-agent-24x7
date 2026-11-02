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

# alive() espera um PID (int) e age() espera um NOME (str).
# A versao anterior passava read() (dict) para os dois -> TypeError sempre,
# marcando o container unhealthy mesmo com o bot rodando.
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=5 \
  CMD python3 -c \
      "from core.beacon import age, alive, pid_of; import sys; sys.exit(0 if alive(pid_of('worker')) and age('worker')<300 else 1)"

CMD ["python3", "main.py", "run"]
