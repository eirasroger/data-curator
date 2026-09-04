# Built from the REPOSITORY ROOT, not from services/api.
#
# That is the whole reason this file exists rather than using
# `gcloud run deploy --source`. A source deploy uploads one directory, so the
# two services could not share domain/ - it would have to be copied into each
# of them and kept in sync by a script. A Dockerfile chooses its own build
# context, so both services just COPY the same domain/ and there is one copy of
# the rules.
FROM python:3.12-slim

# 3.12, pinned, not "latest". The buildpack alternative picks a version for you
# and can change it under your feet between deploys.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies before source: Docker caches layers, and requirements change far
# less often than code does. Copying source first would reinstall every package
# on every one-line edit.
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY domain/ ./domain/
COPY services/api/main.py .

# Cloud Run tells the container which port to listen on via $PORT and it is not
# always 8080. Shell form so the variable is expanded at run time.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
