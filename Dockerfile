FROM python:3.11-slim-trixie AS builder

ARG HERMES_GIT_REF=v2026.9.24

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git init /opt/hermes-agent \
  && cd /opt/hermes-agent \
  && git remote add origin https://github.com/NousResearch/hermes-agent.git \
  && git fetch --depth 1 origin "${HERMES_GIT_REF}" \
  && git checkout --detach FETCH_HEAD \
  && git submodule update --init --recursive --depth 1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

RUN pip install --no-cache-dir --upgrade pip setuptools wheel
# Resolve Hermes and the server together so neither can silently replace an
# exact upstream dependency pin with an incompatible version.
COPY scripts/agent_server/requirements.txt /tmp/agent-server-requirements.txt
COPY scripts/requirements-messaging.txt /tmp/messaging-requirements.txt
COPY scripts/patch_hermes_dependencies.py /tmp/patch_hermes_dependencies.py
# Keep the pinned upstream source commit while applying the reviewed PyJWT
# security compatibility overlay. Exact old metadata is required; provenance
# records the source before/after hashes and the applied patch script hash.
RUN python /tmp/patch_hermes_dependencies.py /opt/hermes-agent
# Upstream's messaging extra requests discord.py[voice], whose PyNaCl<1.6
# constraint is unsafe and requires uv-only overrides. This pip-based image
# installs the same chat adapters without optional Discord voice dependencies.
RUN pip install --no-cache-dir -e "/opt/hermes-agent[cron,pty]" \
  -r /tmp/agent-server-requirements.txt \
  -r /tmp/messaging-requirements.txt \
  && pip check


# radius-cli needs Node.js 22 or later; Debian trixie's nodejs package is 20.
FROM node:22.23.3-trixie-slim AS node


FROM python:3.11-slim-trixie

# radius-cli (npm, from radiustechsystems/tools) is the wallet: keystore,
# balances, transfers, receipts, signing. Pin it so boots are reproducible.
ARG RADIUS_CLI_VERSION=0.5.0
ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/radius-workshop/radius-hermes-railway-template" \
      org.opencontainers.image.revision="${VCS_REF}"

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
  && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    tini \
  && node -e 'if (Number(process.versions.node.split(".")[0]) < 22) throw new Error("radius-cli requires Node.js >=22")' \
  && npm install -g "radius-cli@${RADIUS_CLI_VERSION}" \
  && rm -rf /var/lib/apt/lists/*

# iron-proxy (ironsh/iron-proxy) powers the optional egress service
# (HERMES_SERVICE_ROLE=egress). Same pin as Hermes' `hermes egress`; the
# tarball is checked against the release's SHA-256 before extraction.
ARG IRON_PROXY_VERSION=0.39.0
ARG IRON_PROXY_SHA256_AMD64=0ab2e031074a01410caecaa2769c0b38f69cbdf4b6333bce498fe73919f6f9f6
ARG IRON_PROXY_SHA256_ARM64=1316cb4f20fcfba5042b06d07cd523ffa617de9de2999cd5f1f44474777c48ad
RUN arch="$(dpkg --print-architecture)" \
  && case "$arch" in \
       amd64) sha="${IRON_PROXY_SHA256_AMD64}" ;; \
       arm64) sha="${IRON_PROXY_SHA256_ARM64}" ;; \
       *) echo "unsupported architecture for iron-proxy: $arch" >&2; exit 1 ;; \
     esac \
  && asset="iron-proxy_${IRON_PROXY_VERSION}_linux_${arch}.tar.gz" \
  && curl -fsSL -o "/tmp/${asset}" "https://github.com/ironsh/iron-proxy/releases/download/v${IRON_PROXY_VERSION}/${asset}" \
  && echo "${sha}  /tmp/${asset}" | sha256sum -c - \
  && tar -xzf "/tmp/${asset}" -C /usr/local/bin iron-proxy \
  && chmod 755 /usr/local/bin/iron-proxy \
  && rm -f "/tmp/${asset}" \
  && test -x /usr/local/bin/iron-proxy

# Install Foundry to a stable path that remains available after HOME is remapped.
ARG FOUNDRY_VERSION=v1.8.5
ENV FOUNDRY_DIR=/opt/foundry
RUN curl -fsSL https://foundry.paradigm.xyz | bash \
  && /opt/foundry/bin/foundryup --install "${FOUNDRY_VERSION}"

# Install ByteRover before HOME is remapped to /data so it lands in /root/.local/bin.
# /root is made traversable (not listable) so a non-root UID can run it. The
# install's exit status is not checked (no pipefail), so tolerate a missing
# /root/.local rather than fail the build differently from before.
RUN curl -fsSL https://byterover.dev/install.sh | sh \
  && chmod 0711 /root \
  && if [ -d /root/.local ]; then chmod -R go+rX /root/.local; fi

# Railway runs the image as root (its volumes are root-owned). Kubernetes runs it
# as this UID with fsGroup on /data; see deploy/helm/hermes-agent.
RUN groupadd --system --gid 10001 hermes \
  && useradd --system --uid 10001 --gid 10001 --home-dir /data --no-create-home \
    --shell /usr/sbin/nologin hermes

ENV PATH="/root/.local/bin:/opt/foundry/bin:/opt/venv/bin:${PATH}" \
  PYTHONUNBUFFERED=1 \
  HERMES_HOME=/data/.hermes \
  HOME=/data

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hermes-agent /opt/hermes-agent

WORKDIR /app
COPY VERSION /app/VERSION
COPY scripts/entrypoint.sh /app/scripts/entrypoint.sh
RUN sed -i 's/\r$//' /app/scripts/entrypoint.sh && chmod +x /app/scripts/entrypoint.sh

COPY scripts/godaddy /app/scripts/godaddy
COPY scripts/egress /app/scripts/egress

# Install and build linear-claude-skill (still Node.js) at a pinned commit.
ARG LINEAR_SKILL_REF=d3c9b9ee12a46fe01ac766747385d55ee0e08e0a
RUN git init /app/scripts/linear-skill \
  && cd /app/scripts/linear-skill \
  && git remote add origin https://github.com/radius-workshop/linear-claude-skill \
  && git fetch --depth 1 origin "${LINEAR_SKILL_REF}" \
  && git checkout --detach FETCH_HEAD \
  && npm install --no-fund --no-audit \
  && npm run build \
  && npm prune --omit=dev

# Bootstrap snapshot only: entrypoint copies this to RADIUS_SKILLS_DIR on /data
# when persistent external skills storage is empty. Radius skills live under
# plugins/radius/skills in radiustechsystems/tools.
ARG RADIUS_SKILLS_REF=f7ccf019a1c4cdda4a408612d72dea6c209bd7d1
RUN git init /app/vendor/radius-skills \
  && cd /app/vendor/radius-skills \
  && git remote add origin https://github.com/radiustechsystems/tools.git \
  && git fetch --depth 1 origin "${RADIUS_SKILLS_REF}" \
  && git checkout --detach FETCH_HEAD

COPY scripts/agent_server /app/scripts/agent_server
COPY erc8004_registry /app/erc8004_registry

COPY HERMES.md /app/HERMES.md
COPY README.md /app/README.md

COPY skills /app/skills
COPY plugins /app/plugins

# Preserve the actual build inputs so operators can inspect a deployed image.
COPY scripts/build_info.py /app/scripts/build_info.py
COPY scripts/smoke_image.py /app/scripts/smoke_image.py
RUN python /app/scripts/build_info.py --revision "${VCS_REF}" \
  --radius-cli-version "${RADIUS_CLI_VERSION}" > /app/build-info.json

ENTRYPOINT ["tini", "--"]
CMD ["/app/scripts/entrypoint.sh"]
