# See docker/api.Dockerfile for why this builds from the repository root.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY services/webhook/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY domain/ ./domain/
COPY services/webhook/main.py .

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
