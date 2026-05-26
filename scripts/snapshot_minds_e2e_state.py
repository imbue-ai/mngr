#!/usr/bin/env python3
"""Snapshot a Modal sandbox that already has a minds workspace + Docker
container provisioned, so future test runs can boot from that state nearly
instantly via offload's ``--override-image-id`` flag (offload v0.9.6+).

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
3. Inside the sandbox, start ``dockerd``, ``pnpm install`` the Electron
   deps, and run the existing end-to-end Electron test
   (``apps/minds/test_desktop_client_e2e.py``). That test drives the
   Electron UI to create a forever-claude-template workspace -- the side
   effect we care about is the Docker container the spawned ``mngr create``
   stands up for the agent.
4. Call ``sandbox.snapshot_filesystem()`` to capture the resulting state
   and print the Modal image ID so it can be plumbed into offload as
   ``offload run --override-image-id <ID>``.

We do NOT switch the general mngr_modal provider to ``vm_runtime``: Modal
has capacity issues with it, so it's opt-in for this snapshot workflow only.

Usage:
    uv run python scripts/snapshot_minds_e2e_state.py
    uv run python scripts/snapshot_minds_e2e_state.py --app-name custom-app
    uv run python scripts/snapshot_minds_e2e_state.py --skip-test    # just snapshot a bare image, no agent

The script intentionally lives outside the regular test suite -- it's
expensive (multi-minute), it requires Modal credentials, and it produces a
snapshot ID that downstream tests then reference rather than something CI
would re-derive on every run.
"""

import argparse
import shlex
import sys
import time
from pathlib import Path
from typing import Final

import modal
import modal.exception

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

_DEFAULT_APP_NAME: Final[str] = "mngr-minds-e2e-snapshot"
_SANDBOX_TIMEOUT_SECONDS: Final[int] = 60 * 60
_SNAPSHOT_TIMEOUT_SECONDS: Final[int] = 600
_DOCKER_VERSION: Final[str] = "27.5.1"
_RUNC_VERSION: Final[str] = "v1.3.0"
_NODE_MAJOR: Final[str] = "20"
_PNPM_VERSION: Final[str] = "10"
_CLAUDE_CODE_VERSION: Final[str] = "2.1.141"

# The single end-to-end test that drives Electron to spin up a workspace
# Docker container. We just need to run this once successfully inside the
# sandbox; the resulting /var/lib/docker state is what we want to snapshot.
_E2E_TEST_NODEID: Final[str] = "apps/minds/test_desktop_client_e2e.py::test_create_local_docker_workspace_via_electron"


def _build_snapshot_image() -> modal.Image:
    """Return a Modal image with every dep the minds Electron e2e test needs.

    Built inline (not via ``modal.Image.from_dockerfile``) so this script
    stays self-contained -- ``Dockerfile.release`` is a generated artifact
    that lives outside the repo until ``just _generate-release-dockerfile``
    runs, and we don't want to require that side effect just to take a
    snapshot.
    """
    return (
        modal.Image.debian_slim(python_version="3.12")
        # System deps -- superset of the base mngr Dockerfile, plus the extras
        # the test-docker-electron CI job installs: xvfb (display server for
        # Electron) and the iptables/iproute2 needed by Docker-in-Docker.
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
                "PATH": "/root/.local/bin:/usr/local/bin:/usr/bin:/bin",
                # Avoid `uv sync` symlink-mode bugs that have historically
                # broken Modal snapshotting (see mngr Dockerfile).
                "UV_LINK_MODE": "copy",
                # Pin a stable Playwright browsers path so the test fixture's
                # HOME isolation doesn't hide the baked-in chromium.
                "PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright",
            }
        )
        # Mount the local mngr checkout, then bake `uv sync` + pnpm install
        # into the image so the sandbox boots ready to run the e2e test.
        # `.git` is included so the test's `_current_mngr_branch()` helper
        # works; `.venv` / `node_modules` / `test-results` are excluded
        # because we'll regenerate them inside the image.
        .add_local_dir(
            str(_REPO_ROOT),
            "/code/mngr",
            copy=True,
            ignore=[
                "**/.venv",
                "**/node_modules",
                "**/test-results",
                "**/.test_output",
                "**/__pycache__",
                "**/.pytest_cache",
                "**/.ruff_cache",
                "**/.mypy_cache",
            ],
        )
        .workdir("/code/mngr")
        .run_commands(
            "cd /code/mngr && uv sync --all-packages",
            "cd /code/mngr && uv run --with playwright python -m playwright install --with-deps chromium",
            "cd /code/mngr/apps/minds && pnpm install --frozen-lockfile",
        )
    )


def _exec_in_sandbox(
    sandbox: modal.Sandbox,
    command: str,
    *,
    description: str,
    timeout_seconds: int,
) -> int:
    """Run a shell command inside ``sandbox`` and stream its output."""
    print(f"\n=== [{description}] {command} ===", flush=True)
    proc = sandbox.exec("bash", "-lc", command, timeout=timeout_seconds)
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    for line in proc.stderr:
        sys.stderr.write(line)
        sys.stderr.flush()
    returncode = proc.wait()
    if returncode != 0:
        print(f"=== [{description}] exited {returncode} ===", flush=True)
    return returncode


def _start_dockerd(sandbox: modal.Sandbox) -> None:
    """Bring up dockerd inside the sandbox and wait for the socket."""
    start_script = "/code/mngr/libs/mngr/imbue/mngr/resources/start-dockerd.sh"
    returncode = _exec_in_sandbox(
        sandbox,
        f"chmod +x {shlex.quote(start_script)} && {shlex.quote(start_script)}",
        description="start dockerd",
        timeout_seconds=180,
    )
    if returncode != 0:
        raise RuntimeError(f"start-dockerd.sh failed with returncode {returncode}")


def _run_e2e_test(sandbox: modal.Sandbox) -> None:
    """Run the existing minds Electron e2e test inside the sandbox.

    Mirrors the ``test-docker-electron`` CI job's pytest invocation, which
    wraps the pytest run in ``xvfb-run -a`` and pins the per-test timeout
    to the test's own ``@pytest.mark.timeout(900)``.
    """
    command = (
        "cd /code/mngr && "
        "xvfb-run -a env PYTEST_MAX_DURATION_SECONDS=900 "
        "uv run pytest -n 0 --timeout 900 --no-cov --cov-fail-under=0 -v --tb=short "
        f"{shlex.quote(_E2E_TEST_NODEID)}"
    )
    returncode = _exec_in_sandbox(
        sandbox,
        command,
        description="run minds Electron e2e test",
        timeout_seconds=1500,
    )
    if returncode != 0:
        raise RuntimeError(
            f"Minds Electron e2e test failed with returncode {returncode}; refusing to snapshot a broken state."
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-name",
        default=_DEFAULT_APP_NAME,
        help=f"Modal app name to use (default: {_DEFAULT_APP_NAME!r}).",
    )
    parser.add_argument(
        "--skip-test",
        action="store_true",
        help=(
            "Skip running the Electron e2e test; just snapshot the bare image "
            "with deps installed but no agent container running. Useful for "
            "iterating on the image build before paying the full e2e cost."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    image = _build_snapshot_image()
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

    snapshot_image_id: str | None = None
    try:
        print(f"Sandbox {sandbox.object_id} created.", flush=True)
        _start_dockerd(sandbox)
        if args.skip_test:
            print("--skip-test set; snapshotting without running the e2e test.", flush=True)
        else:
            _run_e2e_test(sandbox)
        snapshot_image_id = _snapshot_sandbox(sandbox)
    finally:
        try:
            sandbox.terminate()
        except modal.exception.Error as exc:
            print(f"Sandbox terminate raised {exc!r}; continuing.", flush=True)

    if snapshot_image_id is None:
        raise SystemExit("Snapshot was not produced; see errors above.")

    print(
        "\nNext step: feed this image id to offload to skip the full image build:\n"
        f"    offload run --override-image-id {snapshot_image_id} ..."
    )


if __name__ == "__main__":
    main()
