"""The box: headless Minds in Docker for a given mngr branch at an exact SHA.

The box is the branch isolation (workspaces themselves always run on Modal). The container is named
minds-box-<branch>-<sha>, so a running box of that name IS that exact mngr -- reuse is idempotent
and never stale. The Modal env is the branch alone (stable across mngr updates).
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]  # apps/mngr_minds_eval
MNGR_REPO = "https://github.com/imbue-ai/mngr.git"
AWS_ENV = Path.home() / ".minds-eval" / "aws.env"
# One fixed Modal env for ALL eval workspaces (any branch/SHA), so clean has a single place to wipe.
# The box stays versioned (minds-box-<branch>-<sha>); only the env is shared.
MODAL_ENV_USER_ID = "evaluator"


class BoxError(RuntimeError):
    pass


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _remote_tip(branch: str) -> str:
    result = _run(["git", "ls-remote", MNGR_REPO, "refs/heads/{}".format(branch)])
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
    """How to actually look at the workspaces: the box (Docker, on this machine) serves the Minds
    dashboard, and its mngr-forward proxy serves each Modal workspace's UI -- both on localhost.
    The proxy has its own auth, so the one-time login URL must be visited once per box. Never prints
    a silent nothing for the login line: if it can't find the URL, it says how to get it."""
    if not is_running(container):
        print("  (box {} is not running)".format(container), flush=True)
        return
    ui = _run(["docker", "exec", container, "printenv", "MINDS_BARE_PORT"]).stdout.strip()
    forward = _run(["docker", "exec", container, "printenv", "MINDS_FORWARD_PORT"]).stdout.strip()
    if not ui:
        return
    print("\n  dashboard:       http://localhost:{}".format(ui), flush=True)
    if forward:
        login = _forward_login(container, int(forward))
        if login:
            print("  workspace login: {}".format(login), flush=True)
            print("                   ^ visit once, then click the workspace in the dashboard", flush=True)
        else:
            print(
                "  workspace login: not emitted yet -- re-run  minds-evals box  once the proxy is up "
                "(or: docker logs {} | grep login)".format(container),
                flush=True,
            )


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c == "-" else "-" for c in text.lower())


def container_name(mngr_branch: str, ref: str) -> str:
    """Box container name -- encodes the exact mngr SHA, so a running box of this name IS that SHA."""
    return "minds-box-{}-{}".format(_slug(mngr_branch), ref[:12])


def find_any_running() -> str:
    """Any running eval box (any branch/SHA). All boxes share the one Modal env, so clean can use any
    of them instead of building a fresh box. '' if none."""
    out = _run(["docker", "ps", "--filter", "name=minds-box-", "--format", "{{.Names}}"]).stdout
    for line in out.splitlines():
        if line.startswith("minds-box-"):
            return line.strip()
    return ""


def resolve(mngr_branch: str) -> tuple[str, str]:
    """(container, ref) for a branch's current remote tip."""
    ref = _remote_tip(mngr_branch)
    return container_name(mngr_branch, ref), ref


def ensure(mngr_branch: str, minds_env: str = "staging") -> str:
    """Build + boot the box for the branch's current tip; return its container name.

    The container name encodes the SHA, so if it is already running it is exactly the right mngr --
    reuse it, no staleness check. All boxes point workspaces at one shared Modal env
    (minds-<env>-evaluator), so clean has a single place to wipe."""
    container, ref = resolve(mngr_branch)
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
            "-p",
            "{}:{}".format(ui, ui),
            "-p",
            "{}:{}".format(forward, forward),
            "-v",
            "{}:/root/.modal.toml:ro".format(Path.home() / ".modal.toml"),
            "-v",
            "{}:/root/.minds-eval/aws.env:ro".format(AWS_ENV),
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
    print("   dashboard:  http://localhost:{}".format(ui), flush=True)
    print("   modal env:  minds-{}-{}  (this box's workspaces spin up here)".format(minds_env, modal_env), flush=True)
    login = _forward_login(container, forward)
    if login:
        print("   workspace login (visit once): {}".format(login), flush=True)
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


def _forward_login(container: str, forward: int, tries: int = 20) -> str:
    """The mngr-forward one-time login URL (SKIP_AUTH covers the dashboard, not the proxy).

    Greps the box's full log history and returns the LATEST match, so it works whether the box just
    booted (poll a bit) or has been up a while (found immediately). tries=1 for an instant lookup.
    """
    import re
    import time

    # Scheme varies by mngr version: plain http, or https when the proxy runs with --use-http2.
    pattern = re.compile(r"https?://localhost:{}/login\?one_time_code=[A-Za-z0-9_-]+".format(forward))
    for attempt in range(tries):
        logs = _run(["docker", "logs", container])
        found = pattern.findall((logs.stdout or "") + (logs.stderr or ""))
        if found:
            return found[-1]
        if attempt < tries - 1:
            time.sleep(2)
    return ""
