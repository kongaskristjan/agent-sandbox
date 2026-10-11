FROM ubuntu:24.04

# Install steps live in docker/. Each script is copied right before its RUN so
# editing one only rebuilds from that layer on.
ARG SCRIPTS=/usr/local/lib/agent-sandbox/build

COPY docker/apt-base.sh $SCRIPTS/
RUN bash $SCRIPTS/apt-base.sh python3 python3-pip python3-venv

RUN useradd -m -s /bin/bash -G root,audio dev

COPY docker/node.sh $SCRIPTS/
RUN bash $SCRIPTS/node.sh

COPY docker/uv.sh $SCRIPTS/
RUN bash $SCRIPTS/uv.sh

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    PLAYWRIGHT_CLI_VERSION=0.1.22 \
    NO_UPDATE_NOTIFIER=1
COPY docker/playwright-cli.sh $SCRIPTS/
RUN bash $SCRIPTS/playwright-cli.sh

# Agent layers go last: `agent-sandbox --update` changes AGENT_CACHE_BUST,
# which re-runs every RUN from here on while the layers above stay cached.
ARG AGENT_CACHE_BUST=0
COPY docker/claude.sh $SCRIPTS/
RUN bash $SCRIPTS/claude.sh

# Every agent is installed in every image and chosen at runtime (SANDBOX_AGENT).
COPY docker/opencode.sh $SCRIPTS/
RUN bash $SCRIPTS/opencode.sh

RUN npm install -g @openai/codex

COPY prepare-auth.py /usr/local/lib/agent-sandbox/prepare-auth.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV UV_PROJECT_ENVIRONMENT=.venv-container \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/home/dev/.cache/uv \
    UV_PYTHON_INSTALL_DIR=/home/dev/.local/share/uv/python

WORKDIR /workspace

ENTRYPOINT ["/entrypoint.sh"]
CMD ["claude", "--dangerously-skip-permissions"]
