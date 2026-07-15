"""The box: a full Minds computer, running as a MODAL SANDBOX -- no local Docker anywhere.

Every box is a desktop: the real Minds Electron app on a virtual display (Xvfb), streamed to the
browser via noVNC through Modal's encrypted tunnel -- you get one https://...modal.host URL, usable
from any machine, and that's the entire networking story. The image is built from docker/Dockerfile
ON MODAL'S BUILDERS (cached per mngr SHA); your machine only makes API calls. `launch` execs the
create flow INSIDE the sandbox (the CLI discovers the app's API port from in there), so the same
computer that creates a batch is the one you watch it in, and `visit-batch` finds it again by tag.

Each box is scoped to ONE Modal env via MNGR__PROVIDERS__MODAL__USER_ID (the batch name), so its
discovery only ever sees that batch's workspaces. The mngr profile's Modal SSH keypair lives on a
shared modal.Volume mounted into every box, so any box can open any workspace. Boxes auto-die at
BOX_TIMEOUT_HOURS (they bill while alive); `minds-evals stop <name>` kills one early.
"""

from __future__ import annotations

import os
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from imbue.mngr_minds_eval import s3_store

# parents[2] of this file = apps/mngr_minds_eval (the image build context).
APP_DIR = Path(__file__).resolve().parents[2]
MNGR_REPO = "https://github.com/imbue-ai/mngr.git"
MODAL_CONFIG = Path.home() / ".modal.toml"

# All box sandboxes live under one Modal app (in the token's default env); the batch scoping is the
# MNGR__PROVIDERS__MODAL__USER_ID env each box gets, not where the box itself lives.
APP_NAME = "minds-eval-boxes"
# One shared Modal SSH keypair for the pinned mngr profile, on a persistent Volume mounted into
# every box: the first box seeds it, later boxes reuse it -> any box can open any workspace.
MNGR_PROFILE = "evaluator"
PROFILE_VOLUME = "minds-eval-modal-profile"
PROFILE_MOUNT = "/root/.minds-staging/mngr/profiles/{}/providers/modal".format(MNGR_PROFILE)

BOX_MEMORY_MB = 16384
BOX_CPUS = 6
# Boxes bill while alive; they self-terminate after this long (visit-batch just makes a new one).
BOX_TIMEOUT_HOURS = 8
NOVNC_PORT = 6080


class BoxError(RuntimeError):
    pass


class ModalEnvExistsError(BoxError):
    """The batch's Modal env already exists -- the eval name was used before."""


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


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


def create_modal_env(user_id: str, minds_env: str = "staging") -> str:
    """Create the batch's Modal env explicitly, as an ATOMIC claim on the eval name: `modal
    environment create` fails if the env exists, which is the uniqueness preflight. Pre-creating it
    also lets every workspace create fan out concurrently. Returns the env name. TERM=dumb because
    modal 1.4.x bleeds ANSI codes into piped output."""

    env_name = modal_env_name(user_id, minds_env)
    child_env = {**os.environ, "TERM": "dumb"}
    result = _run(
        ["uv", "run", "modal", "environment", "create", env_name], cwd=str(APP_DIR.parents[1]), env=child_env
    )
    if result.returncode != 0:
        detail = ((result.stderr or "") + (result.stdout or "")).strip()
        if "already exists" in detail.lower():
            raise ModalEnvExistsError(
                "Modal env {} already exists -- eval names are unique; pick a new name (or delete it: "
                "TERM=dumb uv run python scripts/modal_nuke.py -e {} --force && "
                "TERM=dumb uv run modal environment delete {})".format(env_name, env_name, env_name)
            )
        raise BoxError("could not create Modal env {}: {}".format(env_name, detail[:300]))
    return env_name


def _modal_token_env() -> dict[str, str]:
    """MODAL_TOKEN_ID/SECRET from ~/.modal.toml (the active profile), for the box's own mngr to
    create workspaces on Modal from inside the sandbox."""
    if not MODAL_CONFIG.is_file():
        raise BoxError("missing ~/.modal.toml (Modal auth) -- everything runs on Modal")
    profiles = tomllib.loads(MODAL_CONFIG.read_text())
    active = None
    for profile in profiles.values():
        if isinstance(profile, dict) and profile.get("token_id"):
            if profile.get("active") or active is None:
                active = profile
    if not active:
        raise BoxError("no token in ~/.modal.toml -- run `modal token new`")
    return {"MODAL_TOKEN_ID": str(active["token_id"]), "MODAL_TOKEN_SECRET": str(active["token_secret"])}


def _box_env(user_id: str, ref: str, minds_env: str) -> dict[str, str | None]:
    """Everything the box needs, as plain env vars: its Modal scope, its identity, the Modal token
    (for creating workspaces from inside), and the AWS creds (load_aws_env falls back to env vars
    in-box, so no file is needed)."""
    env: dict[str, str | None] = {
        "MINDS_ENV": minds_env,
        "MNGR__PROVIDERS__MODAL__USER_ID": user_id,
        "MINDS_BOX_MNGR_REF": ref,
        "MINDS_EVAL_IN_BOX": "1",
    }
    env.update(_modal_token_env())
    aws = s3_store.load_aws_env()
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION", "MINDS_EVAL_BUCKET"):
        if aws.get(key):
            env[key] = aws[key]
    return env


def _tags(user_id: str, ref: str) -> dict[str, str]:
    return {"minds-eval-box": user_id, "mngr-ref": ref[:12]}


def _modal():
    # Deliberately lazy: the modal SDK import is heavy and S3-only commands never need it.
    import modal

    return modal


def _app():
    return _modal().App.lookup(APP_NAME, create_if_missing=True)


def find_box(user_id: str, ref: str = ""):
    """The running box sandbox for this batch (and mngr ref, when given), or None."""
    tags = {"minds-eval-box": user_id}
    if ref:
        tags["mngr-ref"] = ref[:12]
    for sandbox in _modal().Sandbox.list(app_id=_app().app_id, tags=tags):
        return sandbox
    return None


def ensure(mngr_branch: str, *, user_id: str, ref: str = "", minds_env: str = "staging"):
    """Build (on Modal) + boot the box sandbox for (mngr ref, user_id); return the modal.Sandbox.

    ref defaults to the branch's current remote tip. Boxes are tagged with (user_id, ref), so if one
    is already running it is exactly the right computer -- reuse it."""
    modal = _modal()
    ref = ref or remote_tip(mngr_branch)
    existing = find_box(user_id, ref)
    if existing is not None:
        print(">> reusing box {} @ mngr {}".format(existing.object_id, ref[:12]), flush=True)
        return existing

    print(
        ">> booting box for {} from mngr {}@{} (image builds on Modal; first time takes minutes)".format(
            user_id, mngr_branch, ref[:12]
        ),
        flush=True,
    )
    image = modal.Image.from_dockerfile(
        path=str(APP_DIR / "docker" / "Dockerfile"),
        context_dir=str(APP_DIR),
        build_args={"MNGR_BRANCH": mngr_branch, "MNGR_REF": ref},
    )
    volume = modal.Volume.from_name(PROFILE_VOLUME, create_if_missing=True)
    sandbox = modal.Sandbox.create(
        "/usr/local/bin/entrypoint.sh",
        app=_app(),
        image=image,
        cpu=BOX_CPUS,
        memory=BOX_MEMORY_MB,
        timeout=BOX_TIMEOUT_HOURS * 3600,
        encrypted_ports=[NOVNC_PORT],
        env=_box_env(user_id, ref, minds_env),
        volumes={PROFILE_MOUNT: volume},
        tags=_tags(user_id, ref),
    )
    _await_ready(sandbox)
    return sandbox


def novnc_url(sandbox) -> str:
    """The box's desktop URL (noVNC through Modal's encrypted tunnel; reachable from anywhere)."""
    tunnel = sandbox.tunnels(timeout=120)[NOVNC_PORT]
    return "{}/vnc.html?autoconnect=true&resize=scale".format(tunnel.url.rstrip("/"))


def _await_ready(sandbox, tries: int = 100) -> None:
    """Poll until the box serves the noVNC page through its tunnel."""
    url = novnc_url(sandbox)
    print(">> waiting for the desktop at {} ...".format(url.split("/vnc.html")[0]), flush=True)
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=5)
            return
        except urllib.error.HTTPError:
            # Any HTTP response (even an error status) means it is serving.
            return
        except (urllib.error.URLError, OSError):
            pass
        if sandbox.poll() is not None:
            raise BoxError("box exited early -- see: modal sandbox logs {}".format(sandbox.object_id))
        time.sleep(3)
    raise BoxError("the box did not come up -- see: modal sandbox logs {}".format(sandbox.object_id))


def write_file(sandbox, path: str, content: str) -> None:
    with sandbox.open(path, "w") as handle:
        handle.write(content)


def run_in_box(sandbox, argv: list[str], extra_env: dict[str, str] | None = None) -> int:
    """Re-run this same CLI inside the box sandbox, streaming its output; return the exit code."""
    command = "cd /work/mngr && uv run --package mngr-minds-eval minds-evals " + " ".join(
        "'{}'".format(a.replace("'", "'\\''")) for a in argv
    )
    process = sandbox.exec("bash", "-lc", command, env=extra_env or {})
    for line in process.stdout:
        print(line, end="", flush=True)
    return process.wait()
