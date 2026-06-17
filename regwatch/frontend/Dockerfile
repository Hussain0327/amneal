FROM node:20-slim

# Patch base-image OS packages before anything else. CI's trivy scan gates the
# build on FIXABLE CRITICAL/HIGH CVEs (--ignore-unfixed), and node:20-slim's
# Debian packages lag the security archive (e.g. libgnutls30, libcap2). Pulling
# the current security updates at build time clears those without pinning to a
# moving base-image digest. Mirrors the API image's apt step.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# node:20-slim ships npm 10.8.2, whose *bundled* deps (tar, glob, minimatch,
# cross-spawn under /usr/local/lib/node_modules/npm) carry fixable CRITICAL/HIGH
# CVEs that trivy flags. Upgrade npm to pull its current bundled set (tar 7.5.x,
# cross-spawn dropped, etc.). This is npm's own toolchain, not the app — the
# app's deps are pinned separately by package-lock.json below.
RUN npm install -g npm@latest

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0"]
