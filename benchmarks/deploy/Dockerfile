# The harness image: `bench` with the Mem0 extra, the tokenizer and the spaCy model baked in, so a run
# downloads nothing but the models it calls. The SDK is this repository's source, so the build context is the
# repository's root:
#   docker build -f benchmarks/deploy/Dockerfile --build-arg HARNESS_COMMIT=$(git rev-parse HEAD) \
#     -t niadra-bench/harness .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH BENCH_ROOT=/repo/benchmarks TIKTOKEN_CACHE_DIR=/opt/tiktoken \
    MEM0_TELEMETRY=false PYTHONUNBUFFERED=1 HOME=/tmp MEM0_DIR=/tmp/.mem0
WORKDIR /repo/benchmarks
COPY pyproject.toml README.md LICENSE /repo/
COPY src /repo/src
COPY benchmarks/pyproject.toml benchmarks/uv.lock ./
RUN uv sync --frozen --no-dev --extra mem0 --no-install-project
COPY benchmarks .
RUN uv sync --frozen --no-dev --extra mem0 \
 && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
 && mkdir -p /repo/benchmarks/results && chown 10001 /repo/benchmarks/results
ARG HARNESS_COMMIT=unknown
ENV BENCH_HARNESS_COMMIT=$HARNESS_COMMIT
USER 10001
ENTRYPOINT ["bench"]
CMD ["--help"]
