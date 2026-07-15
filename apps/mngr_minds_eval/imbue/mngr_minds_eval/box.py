"""The box: a full Minds computer in Docker, pinned to an exact mngr SHA and one Modal env.

A box runs in one of two modes (same image, switched by BOX_MODE at `docker run`):

- headless -- `minds run` serving the Minds HTTP API on MINDS_BARE_PORT. Used by `launch` to create
  a batch's workspaces, then torn down; the workspaces self-complete on Modal.
- desktop  -- the real Minds Electron app on a virtual display (Xvfb), streamed to the browser via
  noVNC. Used by `visit-batch`: you enter the box's desktop and use Minds natively -- open any of
  the batch's workspaces as windows. One published port (noVNC); no host-side tunnels.

Each box is scoped to ONE Modal env via MNGR__PROVIDERS__MODAL__USER_ID (per-batch: the sanitized
batch id), so its discovery only ever sees that batch's workspaces -- small, fast, never OOMs.
The mngr profile (and its Modal SSH keypair) is shared across ALL boxes (`evaluator`), so a visit
box can open workspaces a launch box created.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

# parents[2] of this file = apps/mngr_minds_eval (the docker build context).
APP_DIR = Path(__file__).resolve().parents[2]
MNGR_REPO = "https://github.com/imbue-ai/mngr.git"
AWS_ENV = Path.home() / ".minds-eval" / "aws.env"

# Every box pins the SAME mngr profile and mounts ONE shared Modal SSH keypair, so any box can
# SSH/forward into any workspace regardless of which box created it. (mngr otherwise rolls a random
# per-box profile -> a per-box keypair, so only the creating box could open a workspace.) The keypair
# persists on the host and is seeded by the first box that boots.
MNGR_PROFILE = "evaluator"
SHARED_MODAL_KEYS = Path.home() / ".minds-eval" / "modal-profile" / "providers" / "modal"
ROOT_CONFIG_FILE = Path.home() / ".minds-eval" / "mngr-root-config.toml"
# Cap the box's memory so a runaway process can't take down the whole Docker VM.
BOX_MEMORY = "8g"
# noVNC's fixed port INSIDE a desktop box; published to a free host port at `docker run`.
NOVNC_PORT_IN_BOX = "6080"


class BoxError(RuntimeError):
    pass


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def remote_tip(branch: str) -> str:
    """The branch's current tip SHA on the mngr remote."""
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
    """The Minds API port of a HEADLESS box (used by launch to create workspaces inside it)."""
    result = _run(["docker", "exec", container, "printenv", "MINDS_BARE_PORT"])
    port = result.stdout.strip()
    if not port:
        raise BoxError("container {!r} is not a headless minds box (no MINDS_BARE_PORT)".format(container))
    return port


def sanitize_user_id(text: str) -> str:
    """A batch id -> a Modal user_id (lowercase alnum + dashes, bounded length). The Modal env is
    named minds-<minds_env>-<user_id>, and Modal env names are restrictive."""
    slug = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")[:40].rstrip("-")
    if not slug:
        raise BoxError("cannot derive a Modal user id from {!r}".format(text))
    return slug


def modal_env_name(user_id: str, minds_env: str = "staging") -> str:
    return "minds-{}-{}".format(minds_env, user_id)


def modal_env_exists(user_id: str, minds_env: str = "staging") -> bool | None:
    """Whether the batch's Modal env already exists (the launch preflight: eval names are unique, so
    a pre-existing env means a name collision). None when the listing can't be read -- the caller
    decides whether to proceed on the S3 check alone. TERM=dumb because modal 1.4.x bleeds ANSI
    codes into `--json` output even when piped."""
    target = modal_env_name(user_id, minds_env)
    child_env = {**os.environ, "TERM": "dumb"}
    result = _run(["uv", "run", "modal", "environment", "list", "--json"], cwd=str(APP_DIR.parents[1]), env=child_env)
    if result.returncode != 0:
        return None
    try:
        rows = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    names = {str(row.get("name") or row.get("Name") or "") for row in rows if isinstance(row, dict)}
    return target in names


def container_name(user_id: str, ref: str, desktop: bool) -> str:
    """Box container name -- encodes the env (user_id), the exact mngr SHA, and the mode, so a
    running box of this name IS the right computer for that batch. Reuse is idempotent."""
    return "minds-box-{}-{}{}".format(user_id, ref[:12], "-desktop" if desktop else "")


def novnc_url(container: str) -> str:
    """The host-side noVNC URL of a running desktop box (reads the published port mapping)."""
    out = _run(["docker", "port", container, NOVNC_PORT_IN_BOX]).stdout.strip()
    # e.g. "0.0.0.0:55123" (possibly plus an IPv6 line); take the first port.
    port = out.splitlines()[0].rsplit(":", 1)[-1] if out else ""
    if not port.isdigit():
        raise BoxError("could not read the noVNC port of {} (is it a desktop box?)".format(container))
    return "http://localhost:{}/vnc.html?autoconnect=true&resize=scale".format(port)


def ensure(mngr_branch: str, *, user_id: str, ref: str = "", desktop: bool = False, minds_env: str = "staging") -> str:
    """Build + boot a box for (mngr ref, Modal user_id, mode); return its container name.

    ref defaults to the branch's current remote tip. The container name encodes env + SHA + mode,
    so if it is already running it is exactly the right computer -- reuse it."""
    ref = ref or remote_tip(mngr_branch)
    container = container_name(user_id, ref, desktop)
    if is_running(container):
        print(">> reusing box {} @ mngr {}".format(container, ref[:12]), flush=True)
        return container

    if _run(["docker", "info"]).returncode != 0:
        raise BoxError("Docker daemon is not running -- start Docker")
    if not AWS_ENV.is_file():
        raise BoxError("missing {} -- see SETUP.md".format(AWS_ENV))
    if not (Path.home() / ".modal.toml").is_file():
        raise BoxError("missing ~/.modal.toml (Modal auth) -- workspaces run on Modal")

    tag = "minds-box:{}-{}".format(sanitize_user_id(mngr_branch), ref[:12])

    # Share one Modal SSH keypair across every box (see MNGR_PROFILE): pin the profile via a mounted
    # root config, and mount a persistent host-side keypair dir. The first box to boot writes the
    # keypair into it, later boxes reuse it -> any box can open any workspace.
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
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        container,
        "--memory",
        BOX_MEMORY,
        "--memory-swap",
        BOX_MEMORY,
        # Chromium/Electron uses /dev/shm heavily; docker's 64MB default makes renderers crash.
        "--shm-size",
        "1g",
        "-v",
        "{}:/root/.modal.toml:ro".format(Path.home() / ".modal.toml"),
        "-v",
        "{}:/root/.minds-eval/aws.env:ro".format(AWS_ENV),
        "-v",
        "{}:{}/config.toml:ro".format(ROOT_CONFIG_FILE, mngr_base),
        "-v",
        "{}:{}/profiles/{}/providers/modal".format(SHARED_MODAL_KEYS, mngr_base, MNGR_PROFILE),
        "-e",
        "MINDS_ENV={}".format(minds_env),
        "-e",
        "MNGR__PROVIDERS__MODAL__USER_ID={}".format(user_id),
        "-e",
        "MINDS_BOX_MNGR_REF={}".format(ref),
    ]
    if desktop:
        host_port = _free_port()
        command += ["-e", "BOX_MODE=desktop", "-p", "{}:{}".format(host_port, NOVNC_PORT_IN_BOX)]
        ready_port = host_port
        print(">> starting desktop box {} (noVNC on {})".format(container, host_port), flush=True)
    else:
        api_port = _free_port()
        command += ["-e", "BOX_MODE=headless", "-e", "MINDS_BARE_PORT={}".format(api_port)]
        command += ["-p", "{}:{}".format(api_port, api_port)]
        ready_port = api_port
        print(">> starting headless box {} (Minds API on {})".format(container, api_port), flush=True)
    command.append(tag)

    run = _run(command)
    if run.returncode != 0:
        raise BoxError("docker run failed: {}".format((run.stderr or "").strip()[:300]))
    _await_ready(container, ready_port)
    return container


def _await_ready(container: str, port: int, tries: int = 100) -> None:
    """Poll until the box serves HTTP on the given published port (the Minds API for a headless box,
    the noVNC page for a desktop box)."""
    import time
    import urllib.error
    import urllib.request

    print(">> waiting for the box on port {} ...".format(port), flush=True)
    for _ in range(tries):
        try:
            urllib.request.urlopen("http://localhost:{}/".format(port), timeout=5)
            return
        except urllib.error.HTTPError:
            # Any HTTP response (even an error status) means it is serving.
            return
        except (urllib.error.URLError, OSError):
            pass
        if not is_running(container):
            raise BoxError("box exited early -- docker logs {}".format(container))
        time.sleep(3)
    raise BoxError("the box did not come up -- docker logs {}".format(container))
