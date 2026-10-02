FROM ubuntu:24.04
ARG MEDIAMTX_VERSION=1.12.3
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tar && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_amd64.tar.gz" -o /tmp/mediamtx.tar.gz && \
    tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx && rm /tmp/mediamtx.tar.gz && \
    mkdir -p /app
COPY mediamtx/mediamtx.yml /app/mediamtx.yml
COPY mediamtx/hook.sh /app/hook.sh
RUN chmod +x /app/hook.sh
EXPOSE 8554/tcp 8000/udp 8001/udp 8888/tcp 9997/tcp
ENTRYPOINT ["/usr/local/bin/mediamtx", "/app/mediamtx.yml"]
