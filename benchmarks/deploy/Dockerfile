# The harness image: `bench` with the Mem0 extra, the tokenizer and the spaCy model baked in, so a run
# downloads nothing but the models it calls. Build context: the benchmarks/ directory.
#   docker build -f deploy/Dockerfile --build-arg HARNESS_COMMIT=$(git rev-parse HEAD) -t niadra-bench/harness .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH BENCH_ROOT=/app TIKTOKEN_CACHE_DIR=/opt/tiktoken MEM0_TELEMETRY=false \
    PYTHONUNBUFFERED=1 HOME=/tmp MEM0_DIR=/tmp/.mem0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra mem0 --no-install-project
COPY . .
RUN uv sync --frozen --no-dev --extra mem0 \
 && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
 && mkdir -p /app/results && chown 10001 /app/results
ARG HARNESS_COMMIT=unknown
ENV BENCH_HARNESS_COMMIT=$HARNESS_COMMIT
USER 10001
ENTRYPOINT ["bench"]
CMD ["--help"]
