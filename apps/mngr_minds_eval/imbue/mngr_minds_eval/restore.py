"""Restore a case's post-message snapshot and spin it back up as a local Docker workspace.

restic restore <tag> -> a /mngr tree -> seed a new workspace's repo from its /mngr/code and create
it with launch_mode=DOCKER, so you can open the workspace and click through what the agent built.

Deps (node_modules/.venv) are excluded from the snapshot, so the restored workspace reinstalls
them from the preserved lockfiles on boot -- deterministic, a couple of minutes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from imbue.mngr_minds_eval import s3_store

RESTORE_ROOT = Path("/work/restores")


def _restic_env(env: dict, repo_url: str, password: str) -> dict:
    return {
        **os.environ,
        "RESTIC_REPOSITORY": repo_url,
        "RESTIC_PASSWORD": password,
        "AWS_ACCESS_KEY_ID": env["AWS_ACCESS_KEY_ID"],
        "AWS_SECRET_ACCESS_KEY": env["AWS_SECRET_ACCESS_KEY"],
        "AWS_DEFAULT_REGION": env.get("AWS_DEFAULT_REGION", "us-east-1"),
    }


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:400]}


def restore(batch: str, case_name: str, message_index: int, *, port: str, restic_password: str = "") -> None:
    env = s3_store.load_aws_env()
    client = s3_store.make_client(env)
    bucket = env["MINDS_EVAL_BUCKET"]
    config = s3_store.get_json(client, bucket, "{}/{}".format(batch, s3_store.BATCH_CONFIG_NAME))
    if config is None:
        raise SystemExit("no such batch: {}".format(batch))
    eval_name = config.get("eval_name", "")
    prefix = s3_store.case_prefix(batch, eval_name, case_name)
    repo_url = s3_store.restic_repo_url(env, prefix)
    # The worker uploaded minds' per-workspace password to <case_prefix>/restic_password.
    if not restic_password:
        obj = s3_store.get_text(client, bucket, "{}/restic_password".format(prefix))
        restic_password = (obj or "").strip()
    if not restic_password:
        raise SystemExit(
            "no restic password for {}/{} (the case may not have run yet, or predates password "
            "upload); pass --restic-password".format(batch, case_name)
        )
    tag = "post_message_{}".format(message_index)

    target = RESTORE_ROOT / "{}-{}-{}".format(batch, case_name, tag)
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    print(">> restic restore {} from {}".format(tag, repo_url), flush=True)
    result = subprocess.run(
        ["restic", "restore", "latest", "--tag", tag, "--target", str(target)],
        env=_restic_env(env, repo_url, restic_password), capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit("restic restore failed:\n{}".format((result.stderr or result.stdout)[:600]))

    # The restored tree is a whole /mngr: <target>/mngr/{code,agents,events,...}
    mngr_dir = next((p for p in target.rglob("code") if (p / ".git").exists()), None)
    if mngr_dir is None:
        raise SystemExit("restored snapshot has no code git repo under {}".format(target))
    mngr_dir = mngr_dir.parent
    code_dir = mngr_dir / "code"

    subprocess.run(["git", "-C", str(code_dir), "add", "-A"], capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(code_dir), "-c", "user.email=eval@minds", "-c", "user.name=minds-eval",
         "commit", "-q", "-m", "restore {} {}".format(case_name, tag)],
        capture_output=True, text=True,
    )

    # Workspaces are always Modal (the docker box is the branch isolation; workspaces run in the
    # cloud). Create from the restored code, then seed the rest of /mngr (agent state + chat
    # sessions + events) over it so the workspace shows the conversation as it was, not a fresh agent.
    host_name = "RESTORE-{}-{}-{}".format(eval_name, case_name, message_index)
    print(">> creating modal workspace {} from {}".format(host_name, code_dir), flush=True)
    status, body = _post_json("http://127.0.0.1:{}/api/v1/workspaces".format(port), {
        "git_url": str(code_dir), "host_name": host_name, "branch": "",
        "launch_mode": "MODAL", "ai_provider": "SUBSCRIPTION", "backup_provider": "CONFIGURE_LATER",
    })
    if status != 202:
        raise SystemExit("create failed HTTP {}: {}".format(status, body))

    operation_id = body["operation_id"]
    deadline = time.time() + 1800.0
    agent_id = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:{}/api/v1/workspaces/operations/create/{}".format(port, operation_id), timeout=30
            ) as response:
                info = json.loads(response.read().decode())
        except (urllib.error.URLError, OSError):
            time.sleep(5)
            continue
        if info.get("is_done"):
            agent_id = info.get("agent_id")
            break
        if info.get("error"):
            raise SystemExit("create failed: {}".format(info["error"]))
        time.sleep(5)
    if agent_id is None:
        raise SystemExit("timed out waiting for the restored workspace")

    _seed_agent_state(agent_id, mngr_dir)
    print(">> restored workspace up: {} (agent {})".format(host_name, agent_id), flush=True)
    print("   open it from the dashboard; deps reinstall from the lockfiles on first boot.", flush=True)


def _seed_agent_state(agent_id: str, mngr_dir: Path) -> None:
    """Push the restored agent state/sessions/events into the new sandbox's /mngr.

    The create API only transfers the repo, so a fresh workspace starts with a fresh agent. rsync
    the rest of the restored /mngr over it (mngr rsync is the reliable transport; `mngr exec` is
    not) so the chat history from that turn is present. `code/` is excluded -- create already
    shipped it via the git mirror.
    """
    env = dict(os.environ)
    for provider in ("DOCKER", "AZURE", "AWS", "VULTR", "LIMA", "IMBUE_CLOUD", "GCP", "OVH"):
        env["MNGR__PROVIDERS__{}__IS_ENABLED".format(provider)] = "false"
    print(">> seeding agent state + chat history into the new sandbox ...", flush=True)
    result = subprocess.run(
        ["uv", "run", "mngr", "rsync", "--exclude", "code",
         str(mngr_dir).rstrip("/") + "/", "{}:/mngr/".format(agent_id)],
        cwd="/work/mngr", env=env, capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        print("   WARN: could not seed agent state ({}); the code is restored but the chat will be "
              "empty.\n   {}".format(result.returncode, (result.stderr or "").strip()[:300]), flush=True)
