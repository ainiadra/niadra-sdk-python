# LangMem 0.0.30 (MIT) behind the benchmark's small HTTP service (server.py), every package pinned in
# requirements.txt. LangMem has no server of its own; this image is the process an application would run it in.
#   docker build -t niadra-bench/langmem:0.0.30 deploy/systems/langmem
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-deps -r requirements.txt && useradd --system --uid 10001 app
COPY server.py /app/server.py
USER app
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
