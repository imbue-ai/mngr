#!/usr/bin/env python3
"""Snapshot a Modal sandbox that already has a minds workspace + Docker
container provisioned, so future test runs can boot from that state nearly
instantly via offload's ``--override-image-id`` flag (offload v0.9.7+).

Verified end-to-end against Modal on 2026-05-27. The most recently
verified snapshot id is ``im-01KSMZYQ5X1MKME78EQYRNW6CT`` (the earlier
``im-01KSK6YY0V97VGXJZMCB4S9D12`` works equivalently but lacks the
``/app -> /code/mngr`` symlink the snapshot script now layers in --
offload's ``--override-image-id`` path hardcodes ``workdir="/app"``
on the resumed sandbox, so the symlink is what lets ``uv run pytest``
find the project from offload's chosen workdir).

End-to-end:
``just test-offload-minds-snapshot im-01KSMZYQ5X1MKME78EQYRNW6CT`` runs
the ``minds_snapshot_resume`` test suite (today: one sanity test in
``apps/minds/test_snapshot_resume.py``) against the snapshot in ~17-20s
wall clock per run, 4/4 successive runs green.

A couple of vm_runtime sandboxes did fail intermittently with
exit_code=137 + missing junit.xml during early testing on
2026-05-27. We could not reproduce that on a retry under the same
conditions, so the working hypothesis is a transient Modal-side
vm_runtime flake (the runtime is preview-stage and the operator
warned about capacity issues). If you hit it, just retry; if it
turns into a pattern, capture the failing batch's verbose offload
log and check the modal_sandbox.py exec path before assuming
offload's at fault.

PREREQUISITE: ``experimental_options={"vm_runtime": True}`` requires the
caller's active Modal profile to have the VM-runtime preview enabled. If
``Sandbox.create`` fails with ``MODAL_FUNCTION_RUNTIME must be set to
'gvisor'`` or ``experimental_options['vm_runtime']=True conflicts with
runtime=...``, your profile lacks the preview -- switch to one that
has it (``modal profile activate <name>`` / ``MODAL_PROFILE=<name>``).

This is a one-off demonstration script for the test-efficiency groundwork.
The flow is:

1. Build a Modal image that mirrors what the ``test-docker-electron`` CI
   runner sets up: Python + uv + Docker-in-Docker + Node + pnpm + xvfb +
   Playwright, plus the local mngr repo source.
2. Create a Modal sandbox with ``experimental_options={"vm_runtime": True}``
   -- Modal's true-VM runtime. We need this specifically because
   Docker-in-sandbox state (everything in ``/var/lib/docker``, including
   the agent's container and image layers) only persists across a
   ``snapshot_filesystem()`` call inside a VM-runtime sandbox.
3. Inside the sandbox, start ``dockerd`` and invoke
   ``imbue.minds.desktop_client.e2e_workspace_runner.create_workspace_via_electron``
   directly (no pytest). The runner is the shared driver behind the
   minds Electron e2e test -- driving the Electron UI to create a
   forever-claude-template workspace -- but we call it WITHOUT the
   ``mngr destroy`` cleanup the pytest test wraps it with, so the agent
   and its Docker container survive into the snapshot.
4. Call ``sandbox.snapshot_filesystem()`` to capture the resulting state
   and print the Modal image ID so it can be plumbed into offload as
   ``offload run --override-image-id <ID>``.

We do NOT switch the general mngr_modal provider to ``vm_runtime``: Modal
has capacity issues with it, so it's opt-in for this snapshot workflow only.

Usage:
    uv run python scripts/snapshot_minds_e2e_state.py
    uv run python scripts/snapshot_minds_e2e_state.py --app-name custom-app
    uv run python scripts/snapshot_minds_e2e_state.py --skip-workspace-creation  # bare image, no agent

The script intentionally lives outside the regular test suite -- it's
expensive (multi-minute), it requires Modal credentials, and it produces a
snapshot ID that downstream tests then reference rather than something CI
would re-derive on every run.
"""

import argparse
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Final

import modal
import modal.exception
from modal.stream_type import StreamType

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

_DEFAULT_APP_NAME: Final[str] = "mngr-minds-e2e-snapshot"
_SANDBOX_TIMEOUT_SECONDS: Final[int] = 60 * 60
_SNAPSHOT_TIMEOUT_SECONDS: Final[int] = 600
_DOCKER_VERSION: Final[str] = "27.5.1"
_RUNC_VERSION: Final[str] = "v1.3.0"
_NODE_MAJOR: Final[str] = "20"
_PNPM_VERSION: Final[str] = "10"
_CLAUDE_CODE_VERSION: Final[str] = "2.1.141"

# In-sandbox entrypoint that invokes the shared e2e workspace runner the
# pytest test also uses, but without the test's mngr-destroy cleanup. The
# resulting workspace agent + Docker container is exactly what we want
# baked into the filesystem snapshot.
#
# Two notes on why this is a python -c string instead of a checked-in
# helper script:
# - Keeping the entrypoint adjacent to the snapshot script makes it
#   obvious that this is a one-off operator tool and that any cleanup
#   skip here is *intentional*.
# - The mngr clone inside the sandbox already has the runner under
#   ``imbue.minds.desktop_client.e2e_workspace_runner`` (installed via
#   the image's ``uv sync --all-packages``), so a single import is all
#   we need.
_IN_SANDBOX_RUNNER_PROGRAM: Final[str] = textwrap.dedent(
    """
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    from imbue.minds.desktop_client.e2e_workspace_runner import (
        configure_logging,
        create_workspace_via_electron,
        ensure_minds_env_defaults,
        find_free_port,
        resolve_fct_path,
    )
    from imbue.mngr.utils.testing import get_short_random_string

    configure_logging()
    # Explicit os.environ-mutating setter -- this is a throwaway sandbox so
    # process-global env mutation is fine here. The runner intentionally
    # refuses to default to this so the test path (which uses monkeypatch)
    # can't accidentally leak env vars across tests.
    def _write_to_os_environ(name: str, value: str) -> None:
        os.environ[name] = value
    ensure_minds_env_defaults(setenv=_write_to_os_environ)
    # Snapshot builds are test infrastructure, not a real install, so they
    # must not count toward Latchkey's usage.
    _write_to_os_environ("LATCHKEY_DISABLE_COUNTING", "1")
    with tempfile.TemporaryDirectory(prefix="snapshot-fct-") as scratch:
        fct_path = resolve_fct_path(Path(scratch))
        workspace_name = f"forever-{get_short_random_string()}"
        debug_port = find_free_port()
        print(f"[snapshot] workspace={workspace_name} debug_port={debug_port}", flush=True)
        create_workspace_via_electron(fct_path, workspace_name, debug_port)
        # IMPORTANT: do NOT call destroy_agent_best_effort here. The whole
        # point of this script is to leave the workspace agent + Docker
        # container's on-disk state (volumes, /code, /worktree, the
        # bootstrap-written runtime/, etc.) captured by snapshot_filesystem.
        # But we DO want the container itself stopped cleanly before the
        # snapshot fires, so its filesystem state is consistent (no
        # half-written sqlite WALs, no inflight tmux pty writes, etc.)
        # and so a sandbox booted from the snapshot can `docker start`
        # the container deterministically rather than inheriting a
        # mid-flight running state.
        #
        # `docker stop` sends SIGTERM, waits up to `--time`, then SIGKILL.
        # The FCT container runs tini as PID 1, which propagates SIGTERM
        # to the bootstrap/services/agent processes inside. 60s grace is
        # generous enough for the bootstrap to flush its event log and
        # close the chat agent's claude session cleanly.
        running = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.split()
        for name in running:
            print(f"[snapshot] stopping container {name!r}", flush=True)
            subprocess.run(
                ["docker", "stop", "--time", "60", name],
                check=True,
                timeout=120,
            )
        print(
            f"[snapshot] workspace agent {workspace_name!r} container stopped; ready for snapshot",
            flush=True,
        )
    """
).strip()


# rsync exclusion list applied when staging the local mngr checkout into a
# stable temp dir BEFORE the Modal upload. The staging step exists because
# Modal's add_local_dir errors with ``ExecutionError`` if any source file
# changes mid-upload, and the upload takes long enough (multiple minutes)
# that concurrent writers in the working checkout (stop-hook auto-merges
# of main, autofix writes under .reviewer/, parallel pytest runs writing
# under test-results/, etc.) reliably race the upload and abort it.
# Copying once at the start gives Modal a frozen tree to read from.
#
# Exclusion buckets:
# - regenerated inside the image (.venv / node_modules / build caches)
# - written during the upload by other tools (.reviewer, .claude,
#   test-results, .test_output)
# - .git: worktree ``.git`` is a tiny ``gitdir: <path>`` file pointing at
#   the main repo's .git/worktrees/<id>/ -- that path does not exist
#   inside the sandbox, so no in-sandbox git command would work even if
#   we did upload it. The runner's ``_current_mngr_branch`` tolerates a
#   missing / unusable .git and returns None, routing
#   ``resolve_fct_path`` through the documented "fall back to FCT main"
#   path.
# - .external_worktrees can hold large FCT working trees; we prefer the
#   sandbox to clone FCT fresh from the public remote.
_STAGING_RSYNC_EXCLUDES: Final[tuple[str, ...]] = (
    ".venv",
    "node_modules",
    "test-results",
    ".test_output",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".reviewer",
    ".claude",
    ".external_worktrees",
    ".git",
)


def _stage_repo_to_temp_dir(staging_dir: Path) -> Path:
    """Rsync the local mngr checkout into ``staging_dir`` and return the path.

    Insulates Modal's add_local_dir upload from concurrent writers in the
    live working tree (autofix, stop-hook auto-merges, parallel test runs).
    Without this, ``Modal.Image.add_local_dir`` aborts the run with
    ``ExecutionError: <path> was modified during build process`` as soon
    as any tracked file gets rewritten mid-upload.
    """
    target = staging_dir / "mngr"
    target.mkdir(parents=True, exist_ok=True)
    rsync_command = ["rsync", "-a", "--delete"]
    for pattern in _STAGING_RSYNC_EXCLUDES:
        rsync_command += ["--exclude", pattern]
    # Trailing slash on source so rsync copies *contents* of _REPO_ROOT
    # into target rather than nesting it under target/<repo-name>.
    rsync_command += [f"{_REPO_ROOT}/", f"{target}/"]
    print(
        f"Staging mngr checkout into {target} (excluding {len(_STAGING_RSYNC_EXCLUDES)} pattern(s))",
        flush=True,
    )
    subprocess.run(rsync_command, check=True, timeout=600)
    return target


def _build_snapshot_image(staged_repo: Path) -> modal.Image:
    """Return a Modal image with every dep the minds Electron e2e test needs.

    Built inline (not via ``modal.Image.from_dockerfile``) so this script
    stays self-contained -- ``Dockerfile.release`` is a generated artifact
    that lives outside the repo until ``just _generate-release-dockerfile``
    runs, and we don't want to require that side effect just to take a
    snapshot.

    ``staged_repo`` is the frozen copy produced by
    :func:`_stage_repo_to_temp_dir`. Uploading from there (instead of the
    live working tree) is what keeps Modal's "modified during build"
    check from aborting the run.
    """
    return (
        modal.Image.debian_slim(python_version="3.12")
        # System deps -- superset of the base mngr Dockerfile, plus the extras
        # the test-docker-electron CI job installs: xvfb (display server for
        # Electron) and the iptables/iproute2 needed by Docker-in-Docker.
        #
        # The lib* entries are Electron's runtime GUI dependency set on
        # Debian. GitHub-hosted ubuntu-latest runners have these
        # preinstalled, but debian:slim does not, so Electron exits
        # immediately with ``error while loading shared libraries:
        # libgtk-3.so.0`` without them. List sourced from Electron's own
        # Linux-deps doc and the Playwright "Debian deps for chromium"
        # set, then trimmed to what Electron actually needs at runtime.
        .apt_install(
            "bash",
            "build-essential",
            "ca-certificates",
            "curl",
            "git",
            "git-lfs",
            "gnupg",
            "iproute2",
            "iptables",
            "jq",
            "openssh-server",
            "procps",
            "rsync",
            "tini",
            "tmux",
            "unison",
            "wget",
            "xvfb",
            # Electron GUI runtime deps:
            "libgtk-3-0",
            "libnotify4",
            "libnss3",
            "libxss1",
            "libxtst6",
            "libatspi2.0-0",
            "libdrm2",
            "libgbm1",
            "libxkbcommon0",
            "libasound2",
            "libsecret-1-0",
            "libcups2",
            "libpango-1.0-0",
            "libcairo2",
        )
        # Docker-in-Docker static binaries (mirrors Dockerfile.release.extras).
        .run_commands(
            f"curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-{_DOCKER_VERSION}.tgz "
            "| tar xz -C /usr/local/bin --strip-components=1",
            f"rm -f /usr/local/bin/runc "
            f"&& wget -q https://github.com/opencontainers/runc/releases/download/{_RUNC_VERSION}/runc.amd64 "
            "&& chmod +x runc.amd64 && mv runc.amd64 /usr/local/bin/runc",
            "update-alternatives --set iptables /usr/sbin/iptables-legacy",
            "update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy",
        )
        # Node 20 + pnpm -- for the apps/minds Electron app.
        .run_commands(
            f"curl -fsSL https://deb.nodesource.com/setup_{_NODE_MAJOR}.x | bash -",
            "apt-get install -y nodejs",
            f"npm install -g pnpm@{_PNPM_VERSION}",
        )
        # uv + claude code, matching the versions the mngr Dockerfile pins.
        .run_commands(
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            f"curl -fsSL https://claude.ai/install.sh | bash -s {_CLAUDE_CODE_VERSION}",
        )
        .env(
            {
                # Include the sbin dirs so start-dockerd.sh can find `ip`
                # (/usr/sbin/ip) and `iptables-legacy` (/usr/sbin/iptables-legacy)
                # when invoked via `bash -lc` -- Debian's /etc/profile won't
                # restore the sbin paths if PATH is already set.
                "PATH": "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                # Avoid `uv sync` symlink-mode bugs that have historically
                # broken Modal snapshotting (see mngr Dockerfile).
                "UV_LINK_MODE": "copy",
                # Pin a stable Playwright browsers path so the test fixture's
                # HOME isolation doesn't hide the baked-in chromium.
                "PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright",
            }
        )
        # Mount the staged (frozen) mngr checkout, then bake `uv sync` +
        # pnpm install into the image so the sandbox boots ready to run
        # the e2e workflow. The exclusion buckets above already filtered
        # the rsync, so add_local_dir doesn't need a redundant `ignore`.
        .add_local_dir(
            str(staged_repo),
            "/code/mngr",
            copy=True,
        )
        .workdir("/code/mngr")
        .run_commands(
            "cd /code/mngr && uv sync --all-packages",
            "cd /code/mngr && uv run --with playwright python -m playwright install --with-deps chromium",
            "cd /code/mngr/apps/minds && pnpm install --frozen-lockfile",
            # /app -> /code/mngr symlink so offload's --override-image-id
            # path works: offload v0.9.7's create_from_image hardcodes
            # workdir="/app" when booting a sandbox from a supplied image,
            # but our project lives at /code/mngr. Without this symlink,
            # `uv run pytest` from /app fails with "Failed to spawn:
            # `pytest`" because uv can't find the project venv from /app.
            # Symlinking /app -> /code/mngr makes the hardcoded workdir
            # land at the project root, so test runs Just Work.
            "ln -s /code/mngr /app",
        )
    )


def _exec_in_sandbox(
    sandbox: modal.Sandbox,
    command: str,
    *,
    description: str,
    timeout_seconds: int,
) -> int:
    """Run a shell command inside ``sandbox`` and stream its merged output.

    stderr is merged into stdout at the sandbox level via
    ``stderr=StreamType.STDOUT`` so we only have to drain a single pipe.
    Reading two pipes serially (stdout to completion, then stderr) risks
    a deadlock when the process produces enough stderr to fill that
    pipe's buffer while we are still draining stdout. Merging avoids
    that and also gives us a single, naturally-ordered log stream --
    which is what a human operator actually wants here.
    """
    print(f"\n=== [{description}] {command} ===", flush=True)
    proc = sandbox.exec(
        "bash",
        "-lc",
        command,
        timeout=timeout_seconds,
        stderr=StreamType.STDOUT,
    )
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    returncode = proc.wait()
    if returncode != 0:
        print(f"=== [{description}] exited {returncode} ===", flush=True)
    return returncode


def _start_dockerd(sandbox: modal.Sandbox) -> None:
    """Bring up dockerd inside the sandbox and verify the socket responds.

    ``start-dockerd.sh`` backgrounds ``dockerd`` and exits once
    ``docker info`` succeeds inside the script. On a Modal sandbox the
    bash shell exit code occasionally comes back as -1 to the SDK even
    though the script's own logic ran to completion (the backgrounded
    dockerd child confuses exit-code propagation). So we don't gate on
    the start-script's exit code -- instead we run a follow-up
    ``docker info`` in a fresh exec and only fail if THAT comes back
    non-zero, which is the actual signal we care about.
    """
    start_script = "/code/mngr/libs/mngr/imbue/mngr/resources/start-dockerd.sh"
    _exec_in_sandbox(
        sandbox,
        f"chmod +x {shlex.quote(start_script)} && {shlex.quote(start_script)}",
        description="start dockerd",
        timeout_seconds=180,
    )
    verify_rc = _exec_in_sandbox(
        sandbox,
        "/usr/local/bin/docker info >/dev/null && echo 'dockerd verified up'",
        description="verify dockerd is responsive",
        timeout_seconds=30,
    )
    if verify_rc != 0:
        raise RuntimeError(
            f"`docker info` failed inside the sandbox with returncode {verify_rc} -- "
            "start-dockerd.sh did not actually bring up dockerd."
        )


def _create_workspace_in_sandbox(sandbox: modal.Sandbox) -> None:
    """Drive the Electron flow inside the sandbox via the shared runner.

    Calls ``imbue.minds.desktop_client.e2e_workspace_runner`` directly
    (no pytest) so we can deliberately *omit* the agent-destroy cleanup
    the pytest test wraps that function with. Wrapped in ``xvfb-run -a``
    because Electron needs an X display.
    """
    command = "cd /code/mngr && xvfb-run -a uv run python -c {}".format(shlex.quote(_IN_SANDBOX_RUNNER_PROGRAM))
    returncode = _exec_in_sandbox(
        sandbox,
        command,
        description="create workspace via Electron",
        timeout_seconds=1500,
    )
    if returncode != 0:
        raise RuntimeError(
            f"Workspace creation failed with returncode {returncode}; refusing to snapshot a broken state."
        )


def _snapshot_sandbox(sandbox: modal.Sandbox) -> str:
    """Snapshot the sandbox filesystem and return the Modal image ID."""
    print(
        f"\n=== Snapshotting filesystem (timeout={_SNAPSHOT_TIMEOUT_SECONDS}s) ===",
        flush=True,
    )
    started_at = time.monotonic()
    image = sandbox.snapshot_filesystem(timeout=_SNAPSHOT_TIMEOUT_SECONDS)
    elapsed_seconds = time.monotonic() - started_at
    image_id = image.object_id
    print(f"Snapshot complete in {elapsed_seconds:.1f}s. Image ID: {image_id}", flush=True)
    return image_id


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--app-name",
        default=_DEFAULT_APP_NAME,
        help=f"Modal app name to use (default: {_DEFAULT_APP_NAME!r}).",
    )
    parser.add_argument(
        "--skip-workspace-creation",
        action="store_true",
        help=(
            "Skip the Electron workspace-creation step; just snapshot the bare "
            "image with deps installed and dockerd up but no workspace agent. "
            "Useful for iterating on the image build before paying the full "
            "workspace-creation cost."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # Stage the repo into a temp dir BEFORE building the image so the
    # Modal upload reads from a frozen tree. The staging dir lives for
    # the whole image-build phase; we clean it up after Sandbox.create
    # returns (Modal has already materialized the image at that point,
    # so the staged copy is no longer referenced).
    with tempfile.TemporaryDirectory(prefix="mngr-snapshot-stage-") as staging_dir_str:
        staging_dir = Path(staging_dir_str)
        staged_repo = _stage_repo_to_temp_dir(staging_dir)

        image = _build_snapshot_image(staged_repo)
        app = modal.App.lookup(args.app_name, create_if_missing=True)

        print(f"Creating sandbox in app {args.app_name!r} with vm_runtime=True", flush=True)
        sandbox = modal.Sandbox.create(
            image=image,
            app=app,
            timeout=_SANDBOX_TIMEOUT_SECONDS,
            cpu=4.0,
            memory=8 * 1024,
            # The whole point of this script: opt in to Modal's VM runtime so
            # Docker-in-sandbox state survives snapshot_filesystem(). We are
            # NOT enabling this in the general mngr_modal provider -- Modal
            # has capacity issues with vm_runtime, so this is scoped to the
            # snapshot workflow only.
            experimental_options={"vm_runtime": True},
        )

    try:
        print(f"Sandbox {sandbox.object_id} created.", flush=True)
        _start_dockerd(sandbox)
        if args.skip_workspace_creation:
            print(
                "--skip-workspace-creation set; snapshotting without a workspace agent.",
                flush=True,
            )
        else:
            _create_workspace_in_sandbox(sandbox)
        snapshot_image_id = _snapshot_sandbox(sandbox)
        # Printed inside the try so it only fires when the snapshot
        # actually succeeded. Any failure in the try block propagates
        # through the finally below as the real exception, which is
        # more useful to the operator than a generic "snapshot not
        # produced" string.
        print(
            "\nNext step: feed this image id to offload to skip the full image build:\n"
            f"    offload run --override-image-id {snapshot_image_id} ..."
        )
    finally:
        try:
            sandbox.terminate()
        except modal.exception.Error as exc:
            print(f"Sandbox terminate raised {exc!r}; continuing.", flush=True)


if __name__ == "__main__":
    main()
