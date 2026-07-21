# Digest-pinned base: the tag is mutable (the node image republishes 20-slim on
# every Debian/Node patch), so pin the exact multi-arch index digest for a
# reproducible, tamper-evident build. Bump the digest; resolve with:  docker
# manifest inspect node:20-slim  (the index/list digest, not a per-arch child).
FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

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
# CVEs that trivy flags. Pin a known-good npm whose bundled set is patched:
# 11.18.0 ships tar 7.5.19 (CVE-2026-59873/-59874), brace-expansion 5.0.7
# (CVE-2026-13149), undici 6.27.0 (CVE-2026-12151 — this pin previously had to
# patch npm's bundled undici in place; 11.18.0 bundles the fixed one, so that
# surgery is gone), glob 13, minimatch 10, and drops cross-spawn. npm 12 needs
# Node >=22, so 11.18.0 is the newest pin this node:20 base can take. This is
# npm's own toolchain, not the app — app deps are pinned by package-lock.json
# below. Bump this pin when a future CVE forces it; don't float to @latest, which
# would re-pull an unverified npm on every build (supply-chain surface).
RUN npm install -g npm@11.18.0

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 3000

# Drop privileges: the node base image ships a built-in unprivileged `node`
# user (uid 1000). The dev server writes .next/ under /app and (in compose) into
# the node_modules volume, so /app must be owned by `node`. On a compose
# bind-mount (./regwatch/frontend:/app) the host owns the tree, so that dir must
# be writable by uid 1000 there.
RUN chown -R node:node /app
USER node

CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0"]
