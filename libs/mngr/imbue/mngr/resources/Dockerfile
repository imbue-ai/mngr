FROM python:3.12-slim

# Install system dependencies including tini for proper signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    fd-find \
    git \
    git-lfs \
    jq \
    nano \
    openssh-server \
    procps \
    ripgrep \
    rsync \
    tini \
    tmux \
    unison \
    wget \
    xxd \
    && rm -rf /var/lib/apt/lists/*

# Install ttyd binary from GitHub releases (not available via apt)
RUN ARCH=$(uname -m) && \
    curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${ARCH}" \
    -o /usr/local/bin/ttyd && \
    chmod +x /usr/local/bin/ttyd

RUN mkdir -p -m 755 /etc/apt/keyrings \
	&& out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
	&& cat $out | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
	&& chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
	&& mkdir -p -m 755 /etc/apt/sources.list.d \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
	&& apt update \
	&& apt install gh -y

# Install uv (fast Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && echo 'PATH="/root/.local/bin:$PATH"' >> /root/.bashrc
ENV PATH="/root/.local/bin:$PATH"

# Install claude code (pass CLAUDE_CODE_VERSION as a build arg to pin a specific version)
ARG CLAUDE_CODE_VERSION=""
RUN curl -fsSL https://claude.ai/install.sh > /tmp/install_claude.sh && ( if [ -n "$CLAUDE_CODE_VERSION" ]; then cat /tmp/install_claude.sh | bash -s "$CLAUDE_CODE_VERSION"; else cat /tmp/install_claude.sh | bash; fi && test -x /root/.local/bin/claude ) || ( cat /tmp/install_claude.sh && exit 1 )
ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}

# without this, there are some annoying bugs on modal's side with snapshotting
ENV UV_LINK_MODE=copy

# copy in all of our code (offload exports the repo tree as the build context).
# COPY lands files owned by root:root by default, so no chown is needed.
COPY . /code/mngr/
# --system writes to /etc/gitconfig so the exemption survives tests that
# redirect HOME to a tmp dir (isolate_home). Tests that also set
# GIT_CONFIG_NOSYSTEM=1 still need a per-test-home .gitconfig, which
# isolate_home and isolate_git both now provide.
RUN git config --system --add safe.directory '*'
# Normalize /code/mngr/.git: when the build context originates from a git
# worktree (e.g. a local `git worktree add` checkout), `.git` is a *file*
# containing `gitdir: /path/on/host/.git/worktrees/<name>` instead of a
# directory. That path does not exist inside the sandbox, so any
# `git rev-parse --show-toplevel` from /code/mngr fails with "not a git
# repository". Detect the worktree-style file, drop it, and re-init with
# a single snapshot commit so `git ls-files` and friends still work for
# ratchet tests and mngr CLI commands that need repo discovery.
#
# Also ensure an `origin` remote is present -- offload's export_tree
# invokes `git init` + `git fetch --depth=1` against a file:// URL, which
# does NOT register an origin. Downstream mngr_schedule release tests
# shell out via `make_tar_of_repo.sh` which calls `git remote get-url
# origin`. The URL value itself is only used to derive a GitHub base for
# the packaged tarball, so the precise string doesn't matter -- it just
# has to exist and point at a plausible github.com URL.
RUN if [ -f /code/mngr/.git ]; then \
        echo "Normalizing worktree-style /code/mngr/.git -> fresh in-image .git" ; \
        rm /code/mngr/.git && cd /code/mngr && git init -q . && \
        git add -A && \
        git -c user.email=ci@local -c user.name=ci commit -q -m 'sandbox-init' ; \
    fi && \
    cd /code/mngr && \
    (git remote get-url origin >/dev/null 2>&1 || \
        git remote add origin https://github.com/imbue-ai/mngr.git)

# set working directory to the project root -- this is where `mngr schedule`
# will copy project deploy files and where scheduled commands will run
WORKDIR /code/mngr/

# install python dependencies
RUN unset UV_INDEX_URL && uv sync --all-packages && uv tool install -e /code/mngr/libs/mngr --with-editable /code/mngr/libs/mngr_modal --with-editable /code/mngr/libs/mngr_schedule && uv tool install modal

# Run idly forever while being responsive to SIGTERM.
# PID 1 must explicitly install signal handlers in order to respect signals.
# `tail -f /dev/null` does not do this.
# Since `docker stop` issues a `SIGTERM`, we use an explicit `trap`.
# In practice, this appears to enable rapid interactions using `docker stop`.
CMD ["sh", "-c", "trap 'exit 0' TERM; tail -f /dev/null & wait"]
