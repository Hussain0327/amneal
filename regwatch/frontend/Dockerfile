FROM node:20-slim

# Patch base-image OS packages before anything else. CI's trivy scan gates the
# build on FIXABLE CRITICAL/HIGH CVEs (--ignore-unfixed), and node:20-slim's
# Debian packages lag the security archive (e.g. libgnutls30, libcap2). Pulling
# the current security updates at build time clears those without pinning to a
# moving base-image digest. Mirrors the API image's apt step.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0"]
