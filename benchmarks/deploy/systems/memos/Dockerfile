# MemOS's server API (Apache 2.0) from github.com/MemTensor/MemOS at tag v2.0.34 (commit pinned), built the
# way its docker/Dockerfile builds it; without `--reload`, which that file adds for development.
#   docker build -t niadra-bench/memos:v2.0.34 deploy/systems/memos
FROM python:3.11-slim
ARG MEMOS_COMMIT=41bf5c7fa89ee08ebedf3c662638b06fc29aca8d
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ build-essential libffi-dev \
      python3-dev curl git ca-certificates \
 && git init -q /src && cd /src && git remote add origin https://github.com/MemTensor/MemOS.git \
 && git fetch -q --depth 1 origin "$MEMOS_COMMIT" && git checkout -q FETCH_HEAD \
 && mkdir /app && cp -r /src/docker /app/docker && cp -r /src/src /app/src && rm -rf /src \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --upgrade pip && pip install --no-cache-dir -r docker/requirements.txt
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "memos.api.server_api:app", "--host", "0.0.0.0", "--port", "8000"]
