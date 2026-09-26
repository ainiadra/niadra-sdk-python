# Memobase's API server (Apache 2.0) from github.com/memodb-io/memobase at tag v0.0.42 (commit pinned),
# built the way its src/server/api/Dockerfile builds it. Built from source so the same file serves arm64
# and amd64 (the repository's compose pins its API image to amd64).
#   docker build -t niadra-bench/memobase:v0.0.42 deploy/systems/memobase
FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/
ARG MEMOBASE_COMMIT=358c16bbc6d687937d79bc2f984a11c3be8da901
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && git init -q /src && cd /src && git remote add origin https://github.com/memodb-io/memobase.git \
 && git fetch -q --depth 1 origin "$MEMOBASE_COMMIT" && git checkout -q FETCH_HEAD \
 && mkdir /app && cp /src/src/server/api/pyproject.toml /src/src/server/api/uv.lock /src/src/server/api/api.py /app/ \
 && cp -r /src/src/server/api/memobase_server /app/memobase_server && rm -rf /src \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN uv sync --frozen --no-dev --no-cache-dir
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
