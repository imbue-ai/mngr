FROM python:3.12-slim

# Install system dependencies including tini for proper signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    cron \
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

# Install ttyd binary from GitHub releases (not available via apt).
# Retry with backoff because github.com releases intermittently reset the
# connection when pulled from a Modal builder -- e.g. mngr_schedule release
# tests fail with "curl: (35) Recv failure: Connection reset by peer" on
# the nested deploy image build.
RUN ARCH=$(uname -m) && \
    for attempt in 1 2 3 4 5; do \
        curl -fsSL --retry 3 --retry-delay 5 --retry-connrefused \
            "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${ARCH}" \
            -o /usr/local/bin/ttyd && break ; \
        echo "ttyd download attempt $attempt failed, retrying in $((attempt * 5))s..." ; \
        sleep $((attempt * 5)) ; \
    done && \
    test -s /usr/local/bin/ttyd && \
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

# Install claude code (pass CLAUDE_CODE_VERSION as a build arg to pin a specific version).
# The default must be kept in sync with forever-claude-template's
# .mngr/settings.toml pin (`[agent_types.claude].version`). The release-test
# `test_claude_code_version_matches_forever_claude_template_pin` enforces the
# sync; bump both together when rolling a new claude release.
ARG CLAUDE_CODE_VERSION="2.1.116"
RUN curl -fsSL https://claude.ai/install.sh > /tmp/install_claude.sh && ( if [ -n "$CLAUDE_CODE_VERSION" ]; then cat /tmp/install_claude.sh | bash -s "$CLAUDE_CODE_VERSION"; else cat /tmp/install_claude.sh | bash; fi && test -x /root/.local/bin/claude ) || ( cat /tmp/install_claude.sh && exit 1 )
ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}

# without this, there are some annoying bugs on modal's side with snapshotting
ENV UV_LINK_MODE=copy

# copy in all of our code (offload exports the repo tree as the build context;
# mngr_schedule unpacks its packaged tarball producer-side via
# unpack_current_tarball_in_place; local docker builds use the working tree
# directly). All three deliver a real source tree at /code/mngr/.
# COPY lands files owned by root:root by default, so no chown is needed.
COPY . /code/mngr/

# --system writes to /etc/gitconfig so the exemption survives tests that
# redirect HOME to a tmp dir (isolate_home). Tests that also set
# GIT_CONFIG_NOSYSTEM=1 still need a per-test-home .gitconfig, which
# isolate_home and isolate_git both now provide.
RUN git config --system --add safe.directory '*'
# Normalize /code/mngr/.git to a real in-image git directory. Three input
# shapes must be handled:
#
# 1. `.git` is a *file* (worktree checkout): contains `gitdir: /host/.git/
#    worktrees/<name>` which does not exist inside the sandbox. Drop it
#    and re-init.
# 2. `.git` is missing entirely: happens when the image build context is a
#    tarball produced by `git archive` (e.g. mngr_schedule release tests
#    deploy a scheduled task via `mngr schedule add`, which packages the
#    target repo via git-archive and then rebuilds from that). Init a
#    fresh repo so the rest of this step (and downstream tooling like
#    `git ls-files`) works.
# 3. `.git` is already a directory: leave it alone.
#
# Without this normalization, `git rev-parse --show-toplevel` and
# friends fail from /code/mngr with "not a git repository", which
# breaks ratchet tests and the mngr CLI's repo-discovery paths.
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
    elif [ ! -d /code/mngr/.git ]; then \
        echo "No /code/mngr/.git at all -> fresh in-image .git" ; \
        cd /code/mngr && git init -q . && \
        git add -A && \
        git -c user.email=ci@local -c user.name=ci commit -q -m 'sandbox-init' ; \
    fi && \
    cd /code/mngr && \
    (git remote get-url origin >/dev/null 2>&1 || \
        git remote add origin https://github.com/imbue-ai/mngr.git) && \
    mkdir -p /code/mngr/.mngr && \
    echo "$(git rev-parse HEAD)" > /code/mngr/.mngr/image_commit_hash

# set working directory to the project root -- this is where `mngr schedule`
# will copy project deploy files and where scheduled commands will run
WORKDIR /code/mngr/

# install python dependencies
RUN unset UV_INDEX_URL && uv sync --all-packages && uv tool install -e /code/mngr/libs/mngr --with-editable /code/mngr/libs/mngr_modal --with-editable /code/mngr/libs/mngr_schedule --with-editable /code/mngr/libs/mngr_claude && uv tool install modal

# Run idly forever while being responsive to SIGTERM.
# PID 1 must explicitly install signal handlers in order to respect signals.
# `tail -f /dev/null` does not do this.
# Since `docker stop` issues a `SIGTERM`, we use an explicit `trap`.
# In practice, this appears to enable rapid interactions using `docker stop`.
CMD ["sh", "-c", "trap 'exit 0' TERM; tail -f /dev/null & wait"]
