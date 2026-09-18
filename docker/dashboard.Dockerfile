# Built from the REPOSITORY ROOT, like the others, so it can COPY domain/.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY services/dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY domain/ ./domain/
COPY services/dashboard/main.py .
COPY services/dashboard/templates/ ./templates/

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
