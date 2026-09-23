# Built from the repository root; see docker/api.Dockerfile.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY services/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY domain/ ./domain/
COPY services/worker/main.py .

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
