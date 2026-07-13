"""Launch an eval batch: prepare one FCT clone per case, then create one workspace per case.

Each case's workspace is created with the `api_key` backup provider pointed at the case's own
restic repo in our S3 bucket, so the in-sandbox eval worker can snapshot /mngr per turn and
upload state/transcript -- the run self-completes and everything is retrievable from S3.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

from imbue.mngr_minds_eval import minds_client
from imbue.mngr_minds_eval import s3_store

# The forever-claude-template (workspace template) each eval case is cloned from. The default
# branch carries the eval worker (eval_responder + config.json gating); a branch WITHOUT it won't
# auto-run the conversation or snapshot. Override with --fct-repo / --fct-branch.
DEFAULT_FCT_REPO = "https://github.com/imbue-ai/default-workspace-template.git"
DEFAULT_FCT_BRANCH = "minds-eval-autosend"
CLONES_DIR = Path("/work/clones")
BASE_DIR = Path("/work/eval-base")
BOX_MNGR = Path("/work/mngr")

_VENDOR_EXCLUDES = (".git", ".venv", "node_modules", "__pycache__", "*.pyc", ".pytest_cache",
                    ".mypy_cache", ".ruff_cache", "dist", "build", "*.egg-info", ".coverage")


def _sh(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


# create/poll and the HTTP helpers live in minds_client (shared with workspace + restore).
_api_base = minds_client.api_base
_post_json = minds_client.post_json
_get_json = minds_client.get_json


def load_cases(personas_path: Path) -> list[dict]:
    raw = json.loads(personas_path.read_text())
    cases = raw["personas"] if isinstance(raw, dict) else raw
    if not isinstance(cases, list) or not cases:
        raise ValueError('personas file must be a non-empty list (or {"personas": [...]})')
    out = []
    for index, case in enumerate(cases):
        case_id = str(case.get("id") or "case-{}".format(index + 1))
        prompt = str(case.get("first_prompt", "")).strip()
        if not prompt:
            raise ValueError("case {!r} is missing first_prompt".format(case_id))
        out.append({"id": case_id, "persona": str(case.get("persona", "")).strip(), "first_prompt": prompt})
    return out


def build_create_payload(clone_path: Path, host_name: str, anthropic_key: str, compute: str) -> dict:
    """Create-form fields. Empty branch: a local clone is already on the right commit, and passing
    a branch trips mngr's checkout_branch(FETCH_HEAD) on the use-in-place path.

    backup_provider is configure_later: the eval worker drives restic itself (creds are slotted into
    the clone's config.json), because minds' api_key backup provisioning does not reliably land a
    restic.env inside a Modal sandbox.
    """
    return {
        "git_url": str(clone_path),
        "host_name": host_name,
        "branch": "",
        "launch_mode": compute.upper(),
        "ai_provider": "API_KEY",
        "anthropic_api_key": anthropic_key,
        "backup_provider": "CONFIGURE_LATER",
    }


def _ensure_base(fct_repo: str, fct_branch: str) -> None:
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    print(">> cloning {}@{} (fresh tip)".format(fct_repo, fct_branch), flush=True)
    _sh("git", "clone", "--branch", fct_branch, fct_repo, str(BASE_DIR))


def _vendor_mngr(clone: Path) -> None:
    dest = clone / "vendor" / "mngr"
    dest.mkdir(parents=True, exist_ok=True)
    args = ["rsync", "-a", "--delete"]
    for pattern in _VENDOR_EXCLUDES:
        args += ["--exclude", pattern]
    args += [str(BOX_MNGR).rstrip("/") + "/", str(dest).rstrip("/") + "/"]
    _sh(*args)


def _prepare_clone(case: dict, case_config: dict) -> Path:
    clone = CLONES_DIR / case["id"]
    if clone.exists():
        shutil.rmtree(clone)
    _sh("git", "clone", str(BASE_DIR), str(clone))
    _vendor_mngr(clone)
    (clone / "scripts" / "config.json").write_text(json.dumps(case_config, indent=2))
    _sh("git", "-C", str(clone), "add", "-A")
    _sh("git", "-C", str(clone), "-c", "user.email=eval@minds", "-c", "user.name=minds-eval",
        "commit", "-q", "-m", "eval case {}".format(case["id"]))
    return clone


def destroy_existing_workspace(port: str, host_name: str, timeout: float = 600.0) -> None:
    """Idempotent create: if a workspace with this host name already exists (a re-run with the same
    --name, or an interrupted prior run), destroy it first. mngr registers the host name in the
    Modal environment, so it survives box restarts -- only an actual destroy clears it."""
    try:
        listing = _get_json("{}/api/v1/workspaces".format(_api_base(port)))
    except (urllib.error.URLError, OSError):
        return
    target = host_name.lower()
    match = next((w for w in listing.get("workspaces", [])
                  if (w.get("name") or "").lower() == target and w.get("agent_id")), None)
    if match is None:
        return
    agent_id = match["agent_id"]
    print("     host name in use -- destroying existing {} ({})".format(host_name, agent_id), flush=True)
    status, body = _post_json("{}/api/v1/workspaces/{}/destroy".format(_api_base(port), agent_id), {})
    if status != 202:
        print("     WARN: could not start destroy ({}): {}".format(status, body), flush=True)
        return
    operation_id = body.get("operation_id", agent_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = _get_json("{}/api/v1/workspaces/operations/destroy/{}".format(_api_base(port), operation_id))
        except (urllib.error.URLError, OSError):
            time.sleep(4)
            continue
        if info.get("is_done"):
            print("     destroyed {}".format(host_name), flush=True)
            return
        time.sleep(4)
    print("     WARN: destroy of {} did not confirm in time; create may still collide".format(host_name), flush=True)


def destroy_all_workspaces(port: str) -> None:
    """Clean slate: destroy every workspace the box currently sees. Each destroy tears down the
    Modal sandbox AND removes the host record from the Modal environment, so names free up."""
    try:
        listing = _get_json("{}/api/v1/workspaces".format(_api_base(port)))
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit("could not list workspaces: {}".format(exc))
    workspaces = [w for w in listing.get("workspaces", []) if w.get("agent_id")]
    if not workspaces:
        print(">> no workspaces to destroy", flush=True)
        return
    print(">> destroying {} workspace(s) ...".format(len(workspaces)), flush=True)
    for w in workspaces:
        name, agent_id = w.get("name") or w["agent_id"], w["agent_id"]
        status, body = _post_json("{}/api/v1/workspaces/{}/destroy".format(_api_base(port), agent_id), {})
        if status != 202:
            print("  [ERR ] {}: {}".format(name, str(body)[:150]), flush=True)
            continue
        operation_id = body.get("operation_id", agent_id)
        deadline = time.time() + 600.0
        while time.time() < deadline:
            try:
                info = _get_json("{}/api/v1/workspaces/operations/destroy/{}".format(_api_base(port), operation_id))
            except (urllib.error.URLError, OSError):
                time.sleep(4)
                continue
            if info.get("is_done"):
                print("  [OK  ] destroyed {}".format(name), flush=True)
                break
            time.sleep(4)
        else:
            print("  [WARN] {} did not confirm destroy in time".format(name), flush=True)
    print(">> done", flush=True)




def launch_batch(
    *, eval_name: str, personas_path: Path, anthropic_key: str, num_turns: int, compute: str, port: str, stamp: str,
    mngr_branch: str = "", fct_repo: str = DEFAULT_FCT_REPO, fct_branch: str = DEFAULT_FCT_BRANCH,
) -> dict:
    env = s3_store.load_aws_env()
    client = s3_store.make_client(env)
    bucket = env["MINDS_EVAL_BUCKET"]
    cases = load_cases(personas_path)
    batch = s3_store.batch_prefix(eval_name, stamp)

    print("=" * 66, flush=True)
    print("  EVAL BATCH  {}".format(batch), flush=True)
    print("  cases: {}   turns: {}   compute: {}   bucket: {}".format(len(cases), num_turns, compute, bucket), flush=True)
    print("=" * 66, flush=True)

    # One restic password per batch, stored in the batch config so `restore` can decrypt the repos.
    # We own it (we drive restic ourselves via config.json creds), so there's no minds involvement.
    restic_password = secrets.token_urlsafe(24)
    # mngr_branch is recorded so `restore` rebuilds the SAME box this batch ran on.
    s3_store.put_json(client, bucket, "{}/{}".format(batch, s3_store.BATCH_CONFIG_NAME), {
        "eval_name": eval_name, "created_at": stamp, "num_turns": num_turns,
        "compute": compute, "mngr_branch": mngr_branch, "fct_repo": fct_repo, "fct_branch": fct_branch,
        "restic_password": restic_password, "cases": cases,
    })

    CLONES_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_base(fct_repo, fct_branch)

    results = []
    for index, case in enumerate(cases, 1):
        case_pref = s3_store.case_prefix(batch, eval_name, case["id"])
        host_name = "EVAL-{}-CASE-{}".format(eval_name, case["id"])
        # Everything the in-sandbox worker needs is in config.json (committed into the clone): the
        # S3 target, the restic repo/password, and the scoped AWS creds. This is why the worker does
        # not depend on minds' backup provisioning (which doesn't land a restic.env in the sandbox).
        case_config = {
            "eval_name": eval_name, "case_name": case["id"], "persona": case["persona"],
            "first_prompt": case["first_prompt"], "num_turns": num_turns,
            "s3_bucket": bucket, "s3_prefix": case_pref,
            "restic_repository": s3_store.restic_repo_url(env, case_pref),
            "restic_password": restic_password,
            "aws_access_key_id": env["AWS_ACCESS_KEY_ID"],
            "aws_secret_access_key": env["AWS_SECRET_ACCESS_KEY"],
            "aws_region": env.get("AWS_DEFAULT_REGION", "us-east-1"),
        }
        print("\n  [{}/{}] {}".format(index, len(cases), host_name), flush=True)
        destroy_existing_workspace(port, host_name)  # idempotent: re-run with the same --name works
        clone = _prepare_clone(case, case_config)

        try:
            agent_id = minds_client.create_and_wait(
                port, build_create_payload(clone, host_name, anthropic_key, compute),
                on_stage=lambda s: print("     ... {}".format(s), flush=True),
            )
            results.append({"case": case["id"], "ok": True, "agent_id": agent_id})
            print("     OK agent {}".format(agent_id), flush=True)
        except minds_client.CreateError as exc:
            results.append({"case": case["id"], "ok": False, "error": str(exc)})
            print("     ERR {}".format(exc), flush=True)

    ok = sum(1 for r in results if r.get("ok"))
    print("\n" + "=" * 66, flush=True)
    print("  {}/{} workspaces launched. They self-complete; results land in S3.".format(ok, len(results)), flush=True)
    print("  inspect:  minds-evals inspect {}".format(batch), flush=True)
    print("=" * 66, flush=True)
    if ok == 0:
        sys.exit(1)
    return {"batch": batch, "results": results}
