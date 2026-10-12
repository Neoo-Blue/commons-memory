# Commons memory hub: MCP over streamable HTTP + dashboard.
#   docker build -t commons-memory .
#   docker run -d -p 127.0.0.1:8765:8765 -p 127.0.0.1:8766:8766 -v commons:/data commons-memory
#   docker exec -it <ctr> commons register-agent claude-code     # prints the agent key
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /wheels

FROM python:3.12-slim
ARG EXTRAS="postgres,http,tokens,code"
RUN useradd -m -u 10001 commons && mkdir /data && chown commons /data \
 && apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir "$(ls /wheels/*.whl)[${EXTRAS}]" && rm -rf /wheels
USER commons
ENV COMMONS_DB=/data/commons.db \
    COMMONS_REQUIRE_KEYS=1 \
    COMMONS_HOME=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765 8766
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8765), 3)"
# 0.0.0.0 inside the container; publish ports to 127.0.0.1 or a tunnel. Keys are required (COMMONS_REQUIRE_KEYS=1).
ENTRYPOINT ["commons"]
CMD ["serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8765", "--dashboard", "--dashboard-port", "8766"]
