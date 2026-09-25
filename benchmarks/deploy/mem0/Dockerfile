# Mem0's own self-hosted REST server (Apache 2.0), from the v2.2.0 tag of github.com/mem0ai/mem0, with
# mem0ai 2.2.0 from PyPI and the `nlp` extra plus the spaCy model its README asks for (hybrid search).
# Changed from its server/Dockerfile only where a benchmark needs it: no `--reload`, pinned versions,
# telemetry off, the database migrations before start (as its docker-compose does), and psycopg's
# binary build: at v2.2.0 requirements.txt lists plain `psycopg`, which has no libpq in python:slim
# (the server fails at import); the main branch has since changed it to `psycopg[binary]`.
#   docker build -t niadra-bench/mem0 deploy/mem0
FROM python:3.12-slim
ARG MEM0_REF=v2.2.0
ARG MEM0_COMMIT=47a69e1e72dc562b6fdd49a9ef892229afc7508a
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && git init -q /src && cd /src && git remote add origin https://github.com/mem0ai/mem0.git \
 && git fetch -q --depth 1 origin "$MEM0_COMMIT" && git checkout -q FETCH_HEAD \
 && cp -r /src/server /app && rm -rf /src \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir "mem0ai[nlp]==2.2.0" \
 && pip install --no-cache-dir -r requirements.txt "mem0ai[nlp]==2.2.0" "psycopg[binary]>=3.2.8,<4" \
 && pip install --no-cache-dir \
    "en-core-web-sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
ENV PYTHONUNBUFFERED=1 MEM0_TELEMETRY=false HISTORY_DB_PATH=/tmp/history.db MEM0_SERVER_VERSION=${MEM0_REF}
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn main:app --host 0.0.0.0 --port 8000"]
