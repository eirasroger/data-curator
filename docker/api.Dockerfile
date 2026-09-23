# Built from the repository root so every service can COPY domain/.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, for layer caching.
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY domain/ ./domain/
COPY services/api/main.py .

# Shell form so Cloud Run's $PORT is expanded at run time.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
