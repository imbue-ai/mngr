# open-seer runtime image (DESIGN.md §0.1): one image for the Modal tick, the
# sweep agents, and local Docker testing. Fixers run on fresh images mngr
# provisions — this same image works there too.

FROM python:3.12-slim-bookworm

ARG GITLEAKS_VERSION=8.30.1
# mngr install spec. Default is the published package (the repo's own install
# method: `uv tool install imbue-mngr`). Pin an unreleased branch/ref with:
#   --build-arg MNGR_SPEC='git+https://github.com/imbue-ai/mngr@<ref>#subdirectory=libs/mngr'
ARG MNGR_SPEC=imbue-mngr

# Core tools + mngr's system deps (git, tmux, jq; ssh/rsync for remote hosts).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg jq openssh-client rsync tmux \
    && rm -rf /var/lib/apt/lists/*

# Node 22 (for Claude Code) + gh CLI, both from their official apt repos.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# imbue-code-guardian plugin: the fixer skill's guardian gate (DESIGN §6)
# invokes imbue-code-guardian:autofix, so the plugin must exist on the image.
RUN claude plugin marketplace add imbue-ai/code-guardian \
    && claude plugin install imbue-code-guardian@imbue-code-guardian

# gitleaks ships as a bare release binary — no apt package.
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) gl_arch=x64 ;; arm64) gl_arch=arm64 ;; *) echo "unsupported arch: $arch" >&2; exit 1 ;; esac \
    && curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${gl_arch}.tar.gz" \
        | tar -xz -C /usr/local/bin gitleaks

# uv, then mngr as a uv tool (installs to /root/.local/bin).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV PATH="/root/.local/bin:${PATH}"
RUN uv tool install "${MNGR_SPEC}"

# Shared mngr namespace (DESIGN §2/§9, sentry-sweep SKILL.md §1): every
# sweep/fixer lives under user id "open-seer" in ~/.mngr-open-seer, so
# teammates (scripts/setup_debug_access.py) and later ticks see the same
# agents. Without this each cron container would mint a random user id and
# the overlap guard could never see a previous tick's sweep.
ENV MNGR_HOST_DIR=/root/.mngr-open-seer \
    MNGR_USER_ID=open-seer

WORKDIR /opt/open-seer

# Python runtime deps for app.py (requests, modal) — the base image ships none.
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml

COPY .claude/skills ./.claude/skills
COPY .github ./.github
COPY .gitleaks.toml app.py ./

# Make /opt/open-seer a real git repo: `mngr create`'s default source is the
# current git root, which is how the sweep carries the skills and the SSH key
# registry (.github/open-seer-authorized-keys) into every agent workspace.
RUN git init -q -b main \
    && git add -A \
    && git -c user.email=open-seer@imbue.com -c user.name=open-seer \
        commit -qm "open-seer image snapshot"
