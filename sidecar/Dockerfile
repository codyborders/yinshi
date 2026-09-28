FROM node:26.10.0-slim@sha256:ec7758ee051e457b468b32bde57b0879010b325bb9862718e9615225ce4aaae1

# Debian packages receive security updates at image-build time; the promoted image digest is immutable.
# hadolint ignore=DL3008
RUN apt-get update && \
    # Git remote operations run through an SSH wrapper inside the sidecar.
    # Keep SSH and common user-space install helpers available without sudo.
    apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      git openssh-client \
      jq \
      less \
      pipx \
      procps \
      python3 \
      python3-pip \
      python3-venv \
      ripgrep \
      unzip && \
    rm -rf /var/lib/apt/lists/*

COPY bin/git /usr/local/bin/git
RUN chmod 0755 /usr/local/bin/git && useradd -m -s /bin/bash agent

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && \
    npm rebuild node-pty --foreground-scripts
COPY src/ ./src/

USER agent

CMD ["node", "src/index.js"]
