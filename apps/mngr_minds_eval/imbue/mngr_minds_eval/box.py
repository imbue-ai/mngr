"""The box: headless Minds in Docker for a given mngr branch at an exact SHA.

The box is the branch isolation (workspaces themselves always run on Modal). The container is named
minds-box-<branch>-<sha>, so a running box of that name IS that exact mngr -- reuse is idempotent
and never stale. The Modal env is the branch alone (stable across mngr updates).
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]  # apps/mngr_minds_eval
MNGR_REPO = "https://github.com/imbue-ai/mngr.git"
AWS_ENV = Path.home() / ".minds-eval" / "aws.env"
# One fixed Modal env for ALL eval workspaces (any branch/SHA), so clean has a single place to wipe.
# The box stays versioned (minds-box-<branch>-<sha>); only the env is shared.
MODAL_ENV_USER_ID = "evaluator"

# Every eval box pins the SAME mngr profile and mounts ONE shared Modal SSH keypair, so any box can
# SSH/forward into any workspace in the shared env. (mngr otherwise rolls a random per-box profile ->
# a per-box keypair, so only the box that created a workspace could open it -- other boxes list it but
# reset on open.) The keypair persists on the host and is seeded by the first box that boots.
MNGR_PROFILE = "evaluator"
SHARED_MODAL_KEYS = Path.home() / ".minds-eval" / "modal-profile" / "providers" / "modal"
ROOT_CONFIG_FILE = Path.home() / ".minds-eval" / "mngr-root-config.toml"
# Cap the box's memory so a runaway process can't take down the whole Docker VM.
BOX_MEMORY = "8g"


class BoxError(RuntimeError):
    pass


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _remote_tip(branch: str) -> str:
    try:
        result = _run(["git", "ls-remote", MNGR_REPO, "refs/heads/{}".format(branch)], timeout=30)
    except subprocess.TimeoutExpired:
        raise BoxError("timed out reaching the mngr remote {} -- check your network/VPN".format(MNGR_REPO)) from None
    if result.returncode != 0:
        # A failed ls-remote (offline, auth, DNS) is NOT a missing branch -- surface the real reason.
        detail = (result.stderr or "").strip() or "git ls-remote failed"
        raise BoxError(
            "could not reach the mngr remote {} -- check your network/VPN ({})".format(MNGR_REPO, detail[:200])
        )
    ref = (result.stdout or "").split("\t")[0].strip()
    if not ref:
        raise BoxError("mngr branch {!r} not found on the remote".format(branch))
    return ref


def is_running(container: str) -> bool:
    return _run(["docker", "inspect", "-f", "{{.State.Running}}", container]).stdout.strip() == "true"


def port_of(container: str) -> str:
    result = _run(["docker", "exec", container, "printenv", "MINDS_BARE_PORT"])
    port = result.stdout.strip()
    if not port:
        raise BoxError("container {!r} is not a minds box (no MINDS_BARE_PORT)".format(container))
    return port


def print_view_urls(container: str) -> None:
    """The box's Minds dashboard on localhost. The old per-box forward-login URL is intentionally NOT
    printed: the box's built-in forward eagerly proxies the whole env and OOMs, so that login was
    unreliable. Use `minds-evals view-modal-workspace <name>` for a cheap, scoped, self-authenticating
    view of one workspace instead."""
    if not is_running(container):
        print("  (box {} is not running)".format(container), flush=True)
        return
    ui = _run(["docker", "exec", container, "printenv", "MINDS_BARE_PORT"]).stdout.strip()
    if not ui:
        return
    print("\n  dashboard (http, not https):  http://localhost:{}".format(ui), flush=True)
    print("  view a workspace:             minds-evals view-modal-workspace <name>", flush=True)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c == "-" else "-" for c in text.lower())


def container_name(mngr_branch: str, ref: str) -> str:
    """Box container name -- encodes the exact mngr SHA, so a running box of this name IS that SHA."""
    return "minds-box-{}-{}".format(_slug(mngr_branch), ref[:12])


def find_running_for_branch(mngr_branch: str) -> str:
    """A running box for this branch (any SHA), matched by container-name prefix. '' if none. Lets us
    reuse an existing box without hitting the remote when GitHub is unreachable."""
    prefix = "minds-box-{}-".format(_slug(mngr_branch))
    out = _run(["docker", "ps", "--filter", "name={}".format(prefix), "--format", "{{.Names}}"]).stdout
    for line in out.splitlines():
        if line.strip().startswith(prefix):
            return line.strip()
    return ""


def resolve(mngr_branch: str) -> tuple[str, str]:
    """(container, ref) for a branch's current remote tip."""
    ref = _remote_tip(mngr_branch)
    return container_name(mngr_branch, ref), ref


def _prune_stopped_boxes() -> None:
    """Remove exited eval boxes so they don't accumulate (a box is only needed while running)."""
    ids = _run(["docker", "ps", "-aq", "--filter", "name=minds-box-", "--filter", "status=exited"]).stdout.split()
    if ids:
        _run(["docker", "rm", "-f", *ids])


def modal_env_name(minds_env: str = "staging") -> str:
    return "minds-{}-{}".format(minds_env, MODAL_ENV_USER_ID)


def nuke_modal_env(minds_env: str = "staging") -> None:
    """Clean the shared Modal env to zero, SSH-free. `mngr destroy` SSHes into each sandbox first --
    which fails for workspaces created by a box with a different key -- so we bypass mngr entirely via
    scripts/modal_nuke.py (stops every Modal app + deletes the state volumes in the env). Host-side,
    so it works even when the boxes have died."""
    env = modal_env_name(minds_env)
    monorepo_root = APP_DIR.parents[1]
    script = monorepo_root / "scripts" / "modal_nuke.py"
    if not script.is_file():
        raise BoxError("modal_nuke.py not found at {}".format(script))
    print(">> nuking Modal env {} (stops all sandboxes + deletes volumes) ...".format(env), flush=True)
    # TERM=dumb: modal 1.4.x bleeds ANSI color codes into `modal ... list --json` even when piped,
    # which breaks the script's json.loads; a dumb terminal makes it emit clean JSON.
    child_env = {**os.environ, "TERM": "dumb"}
    # --force: we run non-interactively (captured output), so skip the script's input() confirmation.
    result = _run(["uv", "run", "python", str(script), "-e", env, "--force"], cwd=str(monorepo_root), env=child_env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-300:]
        raise BoxError("modal_nuke failed for env {} (rc={}): {}".format(env, result.returncode, detail))
    print(">> {} cleaned".format(env), flush=True)


def ensure(mngr_branch: str, minds_env: str = "staging") -> str:
    """Build + boot the box for the branch's current tip; return its container name.

    The container name encodes the SHA, so if it is already running it is exactly the right mngr --
    reuse it, no staleness check. All boxes point workspaces at one shared Modal env
    (minds-<env>-evaluator), so clean has a single place to wipe."""
    _prune_stopped_boxes()
    try:
        container, ref = resolve(mngr_branch)
    except BoxError as exc:
        # Remote unreachable (offline / VPN down). If a box for this branch is already running, reuse
        # it rather than fail -- its name encodes a real SHA, so it is a valid box for the branch.
        running = find_running_for_branch(mngr_branch)
        if running:
            print(
                ">> {}\n>> reusing already-running box {} (dashboard http://localhost:{})".format(
                    exc, running, port_of(running)
                ),
                flush=True,
            )
            return running
        raise
    if is_running(container):
        port = port_of(container)
        print(
            ">> reusing box {} @ mngr {} (dashboard http://localhost:{})".format(container, ref[:12], port), flush=True
        )
        return container

    if _run(["docker", "info"]).returncode != 0:
        raise BoxError("Docker daemon is not running -- start Docker Desktop")
    if not AWS_ENV.is_file():
        raise BoxError("missing {} -- see SETUP.md".format(AWS_ENV))
    if not (Path.home() / ".modal.toml").is_file():
        raise BoxError("missing ~/.modal.toml (Modal auth) -- workspaces run on Modal")

    ui, forward = _free_port(), _free_port()
    tag = "minds-box:{}-{}".format(_slug(mngr_branch), ref[:12])
    modal_env = MODAL_ENV_USER_ID  # one shared env for all eval workspaces (the box carries the SHA)

    # Share one Modal SSH keypair across every eval box (see MNGR_PROFILE): pin the profile via a
    # mounted root config, and mount a persistent host-side keypair dir. Created here; the first box to
    # boot writes the keypair into it, later boxes reuse it -> any box can open any workspace.
    SHARED_MODAL_KEYS.mkdir(parents=True, exist_ok=True)
    if not ROOT_CONFIG_FILE.is_file():
        ROOT_CONFIG_FILE.write_text('profile = "{}"\n'.format(MNGR_PROFILE))
    mngr_base = "/root/.minds-{}/mngr".format(minds_env)

    print(">> building {} from mngr {}@{}".format(tag, mngr_branch, ref[:12]), flush=True)
    build = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(APP_DIR / "docker" / "Dockerfile"),
            "--build-arg",
            "MNGR_BRANCH={}".format(mngr_branch),
            "--build-arg",
            "MNGR_REF={}".format(ref),
            "-t",
            tag,
            str(APP_DIR),
        ],
    )
    if build.returncode != 0:
        raise BoxError("docker build failed")

    _run(["docker", "rm", "-f", container])
    print(">> starting box {} (dashboard {}, forward {})".format(container, ui, forward), flush=True)
    run = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--memory",
            BOX_MEMORY,
            "--memory-swap",
            BOX_MEMORY,
            "-p",
            "{}:{}".format(ui, ui),
            "-p",
            "{}:{}".format(forward, forward),
            "-v",
            "{}:/root/.modal.toml:ro".format(Path.home() / ".modal.toml"),
            "-v",
            "{}:/root/.minds-eval/aws.env:ro".format(AWS_ENV),
            "-v",
            "{}:{}/config.toml:ro".format(ROOT_CONFIG_FILE, mngr_base),
            "-v",
            "{}:{}/profiles/{}/providers/modal".format(SHARED_MODAL_KEYS, mngr_base, MNGR_PROFILE),
            "-e",
            "MINDS_BARE_PORT={}".format(ui),
            "-e",
            "MINDS_FORWARD_HOST=0.0.0.0",
            "-e",
            "MINDS_FORWARD_PORT={}".format(forward),
            "-e",
            "MINDS_ENV={}".format(minds_env),
            "-e",
            "MNGR__PROVIDERS__MODAL__USER_ID={}".format(modal_env),
            "-e",
            "MINDS_BOX_MNGR_REF={}".format(ref),
            tag,
        ]
    )
    if run.returncode != 0:
        raise BoxError("docker run failed: {}".format((run.stderr or "").strip()[:300]))

    _await_ready(container, ui)
    print("   dashboard (http):  http://localhost:{}".format(ui), flush=True)
    print("   modal env:  minds-{}-{}  (this box's workspaces spin up here)".format(minds_env, modal_env), flush=True)
    print("   view a workspace:  minds-evals view-modal-workspace <name>", flush=True)
    return container


def _await_ready(container: str, ui: int, tries: int = 100) -> None:
    import time
    import urllib.error
    import urllib.request

    print(">> waiting for Minds on {} ...".format(ui), flush=True)
    for _ in range(tries):
        try:
            urllib.request.urlopen("http://localhost:{}/".format(ui), timeout=5)
            return
        except urllib.error.HTTPError:
            return  # any HTTP response means it is serving
        except (urllib.error.URLError, OSError):
            pass
        if not is_running(container):
            raise BoxError("box exited early -- docker logs {}".format(container))
        time.sleep(3)
    raise BoxError("Minds did not come up -- docker logs {}".format(container))
