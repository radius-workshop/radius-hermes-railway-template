FROM python:3.11-slim AS builder

ARG HERMES_GIT_REF=main

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 --branch "${HERMES_GIT_REF}" --recurse-submodules https://github.com/NousResearch/hermes-agent.git

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -e "/opt/hermes-agent[messaging,cron,cli,pty]"


FROM oven/bun:1.3-slim AS bun-builder

WORKDIR /app/scripts/skills-server
COPY scripts/skills-server/package.json ./
RUN bun install --frozen-lockfile 2>/dev/null || bun install


FROM python:3.11-slim

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    tini \
    nodejs \
    npm \
  && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:${PATH}" \
  PYTHONUNBUFFERED=1 \
  HERMES_HOME=/data/.hermes \
  HOME=/data

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hermes-agent /opt/hermes-agent
COPY --from=oven/bun:1.3-slim /usr/local/bin/bun /usr/local/bin/bun

WORKDIR /app
COPY scripts/entrypoint.sh /app/scripts/entrypoint.sh
RUN sed -i 's/\r$//' /app/scripts/entrypoint.sh && chmod +x /app/scripts/entrypoint.sh

COPY scripts/radius /app/scripts/radius
RUN cd /app/scripts/radius && npm install --omit=dev --no-fund --no-audit

COPY scripts/skills-server /app/scripts/skills-server
COPY --from=bun-builder /app/scripts/skills-server/node_modules /app/scripts/skills-server/node_modules

COPY skills /app/skills

ENTRYPOINT ["tini", "--"]
CMD ["/app/scripts/entrypoint.sh"]
