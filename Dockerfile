# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MPE_DASH_HOST=0.0.0.0

WORKDIR /app

# Dependencies first, so a source edit does not invalidate the wheel cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY market_pulse_engine/ ./market_pulse_engine/
COPY run.py pyproject.toml README.md ./

# The database and logs live on a volume; see docker-compose.yml.
RUN mkdir -p /app/data /app/logs

EXPOSE 8050

# Fails while the first cycle is still running, which is the correct signal.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8050/').read()" || exit 1

CMD ["python", "run.py", "--backfill"]
