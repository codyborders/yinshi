FROM node:20-slim

RUN apt-get update && \
    # Git remote operations run through an SSH wrapper inside the sidecar.
    # Keep the SSH client in the image so push and fetch work against remotes.
    apt-get install -y --no-install-recommends git openssh-client && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash agent

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --production
COPY src/ ./src/

USER agent

CMD ["node", "src/index.js"]
