# syntax=docker/dockerfile:1
# Commons memory hub: MCP (streamable HTTP) + A2A + agent bus shim on :8765, dashboard on :8766.
#   docker run -d --name commons -p 127.0.0.1:8765:8765 -p 127.0.0.1:8766:8766 -v commons-data:/data \
#     -e COMMONS_DASHBOARD_TOKEN=change-me ghcr.io/neoo-blue/commons-memory:latest
#   docker exec commons commons register-agent claude-code      # prints the agent key once
# Multi-arch: docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/neoo-blue/commons-memory:TAG --push .

ARG PYTHON=3.12

# ---------- build: wheel + every dependency into a venv (compilers stay here)
FROM python:${PYTHON}-slim AS build
ARG EXTRAS="postgres,http,tokens,code,otel,sftp,smb,ws,embed"
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# all extras ship manylinux/aarch64 wheels, so no compiler is needed
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m venv /opt/commons \
 && /opt/commons/bin/pip install build \
 && python -m pip install build && python -m build --wheel --outdir /wheels \
 && /opt/commons/bin/pip install "$(ls /wheels/*.whl)[${EXTRAS}]" \
 && /opt/commons/bin/pip uninstall -y build pip setuptools wheel || true
# bake the default embedding model into the image (no download on first start; works offline)
RUN FASTEMBED_CACHE_PATH=/opt/models /opt/commons/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

# ---------- runtime
FROM python:${PYTHON}-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="commons-memory" \
      org.opencontainers.image.description="Unified cross-AI memory, orchestration, messaging and shared drive (MCP hub)" \
      org.opencontainers.image.source="https://github.com/Neoo-Blue/commons-memory" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
RUN sed -i "s|http://deb.debian.org|https://deb.debian.org|g" /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends tini git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin commons \
 && mkdir -p /data/drive /data/backups /run/secrets && chown -R commons:commons /data
COPY --from=build /opt/commons /opt/commons
COPY --from=build /opt/models /opt/models
COPY docker/entrypoint.sh /usr/local/bin/commons-entrypoint
RUN chmod 0755 /usr/local/bin/commons-entrypoint
ENV PATH=/opt/commons/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    COMMONS_HOME=/data \
    COMMONS_DB=/data/commons.db \
    COMMONS_DRIVE_DIR=/data/drive \
    COMMONS_BACKUP_DIR=/data/backups \
    COMMONS_REQUIRE_KEYS=1 \
    COMMONS_HOST=0.0.0.0 \
    COMMONS_PORT=8765 \
    COMMONS_DASHBOARD_PORT=8766 \
    FASTEMBED_CACHE_PATH=/opt/models
USER commons
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8765 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${COMMONS_PORT}/bus/health" >/dev/null || exit 1
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "commons-entrypoint"]
CMD ["serve"]
