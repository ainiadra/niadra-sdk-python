# Supermemory local: the MIT `supermemory-server` binary of release server-v0.0.8
# (github.com/supermemoryai/supermemory/releases), the file `npx supermemory local` installs, checked
# against the release's own SHA-256 files, for the builder's architecture.
#   docker build -t niadra-bench/supermemory:server-v0.0.8 deploy/systems/supermemory
FROM debian:bookworm-slim
ARG TARGETARCH
ARG RELEASE=server-v0.0.8
ARG SHA256_X64=87f32433d0179be80bb9d8a1bafbac65af4128324342a27ecb8bd1a77b5506f3
ARG SHA256_ARM64=eeb9e62a8bf59646bd799a05d1a2981b945413a03640c3a39f4f267ad2d2bf37
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && case "${TARGETARCH:-amd64}" in \
      amd64) FILE=supermemory-server-linux-x64; SUM="$SHA256_X64" ;; \
      arm64) FILE=supermemory-server-linux-arm64; SUM="$SHA256_ARM64" ;; \
      *) echo "unsupported architecture ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /usr/local/bin/supermemory-server \
      "https://github.com/supermemoryai/supermemory/releases/download/${RELEASE}/${FILE}" \
 && echo "$SUM  /usr/local/bin/supermemory-server" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/supermemory-server \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
ENV SUPERMEMORY_DATA_DIR=/data SUPERMEMORY_NO_UPDATE_CHECK=1 SUPERMEMORY_NO_STARTUP_ANIMATION=1 \
    SUPERMEMORY_NO_OPEN=1 SUPERMEMORY_DISABLE_TELEMETRY=1
EXPOSE 6767
CMD ["supermemory-server"]
