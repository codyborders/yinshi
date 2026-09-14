FROM node:26.8-slim@sha256:14bf3eac4bf209d906d3c41256597d3ab1f926b2e93a79e9bdfe1efd32454239

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
