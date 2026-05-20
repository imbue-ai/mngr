# Single-stage Dockerfile. mngr's Modal image builder rejects multi-stage
# Dockerfiles (libs/mngr_modal/imbue/mngr_modal/instance.py: assert not
# dfp.is_multistage), so the offload binary is built inside this stage
# rather than COPY'd from a separate builder stage.
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

# --system writes to /etc/gitconfig so the exemption survives tests that
# redirect HOME to a tmp dir (isolate_home). Tests that also set
# GIT_CONFIG_NOSYSTEM=1 still need a per-test-home .gitconfig, which
# isolate_home and isolate_git both now provide.
RUN git config --system --add safe.directory '*'

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
ARG CLAUDE_CODE_VERSION="2.1.141"
RUN curl -fsSL https://claude.ai/install.sh > /tmp/install_claude.sh && ( if [ -n "$CLAUDE_CODE_VERSION" ]; then cat /tmp/install_claude.sh | bash -s "$CLAUDE_CODE_VERSION"; else cat /tmp/install_claude.sh | bash; fi && test -x /root/.local/bin/claude ) || ( cat /tmp/install_claude.sh && exit 1 )
ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}

# without this, there are some annoying bugs on modal's side with snapshotting
ENV UV_LINK_MODE=copy

# Install the offload binary in the final image. Offload >=0.9.0 expects
# to invoke `offload apply-diff` inside the sandbox image; without the
# binary present, offload falls back to a full image rebuild on every run,
# defeating the checkpoint cache. Keep OFFLOAD_VERSION in sync with the
# offload version pinned in .github/workflows/ci.yml.
#
# rustup is fetched, used once for `cargo install offload`, and then both
# the rustup toolchain and the cargo cache are deleted -- offload is the
# only cargo-installed tool in the image, so the toolchain has no further
# purpose. If a future tool needs to be cargo-installed, install it in
# this same RUN before the cleanup line.
# OFFLOAD_VERSION is exported inline (not a Dockerfile ARG) because mngr_modal
# ships each Dockerfile instruction to Modal as its own `dockerfile_commands`
# call (per-layer caching, see `_build_image_from_dockerfile_contents`). ARGs
# don't carry across separate calls, so an ARG above would expand to empty in
# the RUN below and `cargo install offload@` would error.
RUN export OFFLOAD_VERSION=0.9.5 \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain stable --profile minimal --no-modify-path \
    && /root/.cargo/bin/cargo install offload@${OFFLOAD_VERSION} --locked --root /opt/offload \
    && cp /opt/offload/bin/offload /usr/local/bin/offload \
    && rm -rf /opt/offload /root/.cargo /root/.rustup

# Set working directory to the project root before the COPY so the post-
# source-setup script (and any user shells) start at the same path that
# `mngr schedule` and offload's post_patch_cmd assume.
WORKDIR /code/mngr/

# Pull in just scripts/post-source-setup.sh first, so the post-COPY
# RUN below can invoke it even in the keyframe build-context shape
# where everything else lives inside current.tar.gz. The real-source-
# tree shape would also satisfy this COPY from the same path, and the
# subsequent bulk-copy step plus the in-script tarball extraction
# both leave this same script on disk afterwards -- a self-consistent
# end state.
COPY scripts/post-source-setup.sh /code/mngr/scripts/post-source-setup.sh

# Copy the build context. Two shapes are supported (the shape is
# detected and normalized inside scripts/post-source-setup.sh below):
#   - Real source tree (offload's exported repo, mngr_schedule's
#     producer-side-extracted tree, the test_modal_create release
#     test's unpacked tree, local docker builds).
#   - .mngr/dev/build/ keyframe directory containing only
#     current.tar.gz + a .checkpoint marker + a scripts/ subdir with
#     just the post-source-setup.sh script (populated by
#     make_tar_of_repo.sh; typical end-user flow via
#     .mngr/settings.toml's pre_command_scripts hook + create_templates).
# COPY lands files owned by root:root by default, so no chown is needed.
COPY . /code/mngr/

# THE single, only post-COPY RUN. All source-dependent setup
# (current.tar.gz extraction, .git normalization, image_commit_hash,
# uv sync, editable installs) lives in scripts/post-source-setup.sh.
# That same script also runs as offload's post_patch_cmd in
# offload-modal*.toml so offload's checkpoint+thin-diff path produces
# the same image state as a from-scratch Dockerfile build. Adding any
# other RUN step below this line causes drift between the two paths.
# Don't.
RUN bash scripts/post-source-setup.sh

# Run idly forever while being responsive to SIGTERM.
# PID 1 must explicitly install signal handlers in order to respect signals.
# `tail -f /dev/null` does not do this.
# Since `docker stop` issues a `SIGTERM`, we use an explicit `trap`.
# In practice, this appears to enable rapid interactions using `docker stop`.
CMD ["sh", "-c", "trap 'exit 0' TERM; tail -f /dev/null & wait"]
