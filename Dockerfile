# Stage 1: Build agy-server (official Jetski reverse proxy & bundle rewriter)
FROM golang:1.25-bookworm AS builder

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/ ./cmd/
COPY internal/ ./internal/
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/agy-server ./cmd/agy-server

# Stage 2: Runtime image with official Antigravity CLI (1.4.7) + official Antigravity Hub language_server (2.26.0-6546645232517120)
FROM debian:bookworm-slim

ARG AGY_VERSION="1.4.7"
ARG AGY_URL="https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.7-5900205422411776/linux-x64/cli_linux_x64.tar.gz"
ARG AGY_SHA512="beda760587589f5827b0b516c3ec2c5bd5cd2e91eb0e2533807ba1e990a27dcd63aef0191e0b76fb60feab0d16eaa8210fb79842c34dd4809e257e1712edd152"

ARG AGY_HUB_VERSION="2.26.0-6546645232517120"
ARG AGY_HUB_URL="https://storage.googleapis.com/antigravity-public/antigravity-hub/2.26.0-6546645232517120/linux-x64/Antigravity.tar.gz"
ARG AGY_HUB_SHA256="9f79730372f685d849b935187231edea5b38c5269e855e345e68b5c6071f7884"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    procps \
    lsof \
    iproute2 \
    ripgrep \
    python3 \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install and verify pinned official Google Antigravity CLI (agy 1.4.7)
RUN set -eux; \
    mkdir -p /tmp/agy-install; \
    curl -fsSL "${AGY_URL}" -o /tmp/agy-install/agy.tar.gz; \
    echo "${AGY_SHA512}  /tmp/agy-install/agy.tar.gz" | sha512sum -c -; \
    tar -xzf /tmp/agy-install/agy.tar.gz -C /tmp/agy-install antigravity; \
    install -m 0755 /tmp/agy-install/antigravity /usr/local/bin/agy; \
    rm -rf /tmp/agy-install; \
    /usr/local/bin/agy --version | grep -Fx "${AGY_VERSION}"

# Download and verify the pinned official Antigravity Hub language_server tarball (2.26.0-6546645232517120)
RUN set -eux; \
    mkdir -p /opt/agy-hub; \
    curl -fsSL "${AGY_HUB_URL}" -o /tmp/agy-hub.tar.gz; \
    echo "${AGY_HUB_SHA256}  /tmp/agy-hub.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/agy-hub.tar.gz -C /opt/agy-hub --strip-components=3 Antigravity-x64/resources/bin/language_server; \
    rm -f /tmp/agy-hub.tar.gz; \
    chmod 0755 /opt/agy-hub/language_server

# Non-root worker user (UID 1000 for HF Spaces)
RUN groupadd -g 1000 worker \
    && useradd -m -u 1000 -g 1000 -s /bin/bash worker \
    && mkdir -p /home/worker/workspace /home/worker/.gemini/antigravity-cli /home/worker/.local/share/agy-server \
    && chown -R 1000:1000 /home/worker

COPY --from=builder --chown=1000:1000 /out/agy-server /usr/local/bin/agy-server
COPY --chown=1000:1000 agy-persist.py /usr/local/bin/agy-persist
COPY --chown=1000:1000 entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/agy-server /usr/local/bin/agy-persist /usr/local/bin/entrypoint.sh

USER 1000:1000
WORKDIR /home/worker/workspace

ENV HOME=/home/worker \
    USER=worker \
    PORT=7860 \
    AGY_LS_DIR=/opt/agy-hub \
    AGY_WORKSPACE=/home/worker/workspace \
    PATH="/usr/local/bin:/usr/bin:/bin"

EXPOSE 7860

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
