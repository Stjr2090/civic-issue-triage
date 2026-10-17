FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY static/ ./static/

ENV DATABASE_PATH=/data/civic.db
ENV PORT=8000

EXPOSE 8000
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('PORT', '8000') + '/api/stats')" || exit 1

# Seed is INSERT OR IGNORE, so it is safe to run on every container start.
CMD ["sh", "-c", "python scripts/seed.py && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
