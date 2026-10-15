FROM python:3.11-slim AS builder

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
RUN pip install --no-cache-dir -e "/opt/hermes-agent[messaging,cron,cli,pty]"

# Install the same direct dependencies exercised by CI.
COPY scripts/agent_server/requirements.txt /tmp/agent-server-requirements.txt
RUN pip install --no-cache-dir -r /tmp/agent-server-requirements.txt \
  && pip check


FROM python:3.11-slim

# radius-cli (npm, from radiustechsystems/tools) is the wallet: keystore,
# balances, transfers, receipts, signing. Pin it so boots are reproducible.
ARG RADIUS_CLI_VERSION=0.4.0
ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/radius-workshop/radius-hermes-railway-template" \
      org.opencontainers.image.revision="${VCS_REF}"

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    tini \
    nodejs \
    npm \
  && npm install -g "radius-cli@${RADIUS_CLI_VERSION}" \
  && rm -rf /var/lib/apt/lists/*

# Install Foundry to a stable path that remains available after HOME is remapped.
ENV FOUNDRY_DIR=/opt/foundry
RUN curl -fsSL https://foundry.paradigm.xyz | bash \
  && /opt/foundry/bin/foundryup

# Install ByteRover before HOME is remapped to /data so it lands in /root/.local/bin
RUN curl -fsSL https://byterover.dev/install.sh | sh

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

# Install and build linear-claude-skill (still Node.js)
RUN git clone --depth 1 https://github.com/radius-workshop/linear-claude-skill /app/scripts/linear-skill \
  && cd /app/scripts/linear-skill \
  && npm install --no-fund --no-audit \
  && npm run build \
  && npm prune --omit=dev

# Bootstrap snapshot only: entrypoint copies this to RADIUS_SKILLS_DIR on /data
# when persistent external skills storage is empty.
RUN git clone --depth 1 https://github.com/radiustechsystems/skills.git /app/vendor/radius-skills

COPY scripts/agent_server /app/scripts/agent_server
COPY erc8004_registry /app/erc8004_registry

COPY HERMES.md /app/HERMES.md
COPY AGENTS.md /app/AGENTS.md
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
