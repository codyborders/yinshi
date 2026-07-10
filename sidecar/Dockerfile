FROM node:26.5.0-slim@sha256:ffc78385a788964bb3cbab5e434ff79a10bdc25b8ae6db03fe5fe6cb14053c09

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
RUN npm ci --omit=dev
COPY src/ ./src/

USER agent

CMD ["node", "src/index.js"]
