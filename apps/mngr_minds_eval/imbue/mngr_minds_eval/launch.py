"""Launch an eval batch: prepare one FCT clone per case, then create one workspace per case.

Each case's workspace is created with the `api_key` backup provider pointed at the case's own
restic repo in our S3 bucket, so the in-sandbox eval worker can snapshot /mngr per turn and
upload state/transcript -- the run self-completes and everything is retrievable from S3.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

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


def _api_base(port: str) -> str:
    return "http://127.0.0.1:{}".format(port)


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:400]}


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode())


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


def backup_env_block(env: dict, repo_url: str) -> str:
    """The KEY=VALUE block the create form's api_key backup provider injects into restic.env.

    NOT the password: minds assigns each workspace its own random repository password (and rejects a
    caller-supplied one). The eval worker reads that generated password from restic.env inside the
    sandbox and uploads it to the case's S3 prefix, so restore can decrypt the repo even after the
    box and sandbox are gone.
    """
    return "\n".join([
        "RESTIC_REPOSITORY={}".format(repo_url),
        "AWS_ACCESS_KEY_ID={}".format(env["AWS_ACCESS_KEY_ID"]),
        "AWS_SECRET_ACCESS_KEY={}".format(env["AWS_SECRET_ACCESS_KEY"]),
        "AWS_DEFAULT_REGION={}".format(env.get("AWS_DEFAULT_REGION", "us-east-1")),
    ])


def build_create_payload(
    clone_path: Path, host_name: str, anthropic_key: str, compute: str, backup_env: str
) -> dict:
    """Create-form fields. Empty branch: a local clone is already on the right commit, and passing
    a branch trips mngr's checkout_branch(FETCH_HEAD) on the use-in-place path."""
    return {
        "git_url": str(clone_path),
        "host_name": host_name,
        "branch": "",
        "launch_mode": compute.upper(),
        "ai_provider": "API_KEY",
        "anthropic_api_key": anthropic_key,
        "backup_provider": "API_KEY",
        "backup_api_key_env": backup_env,
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


def _await_create(port: str, operation_id: str, timeout: float = 1800.0) -> dict:
    deadline = time.time() + timeout
    last_stage = ""
    while time.time() < deadline:
        try:
            info = _get_json("{}/api/v1/workspaces/operations/create/{}".format(_api_base(port), operation_id))
        except (urllib.error.URLError, OSError):
            time.sleep(5)
            continue
        stage = info.get("status_text") or info.get("status") or ""
        if stage and stage != last_stage:
            print("     ... {}".format(stage), flush=True)
            last_stage = stage
        if info.get("is_done"):
            return {"ok": True, "agent_id": info.get("agent_id")}
        if info.get("error"):
            return {"ok": False, "error": info.get("error")}
        time.sleep(5)
    return {"ok": False, "error": "timed out"}


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

    # mngr_branch is recorded so `restore` rebuilds the SAME box this batch ran on. The restic
    # password is NOT set here: minds assigns each workspace its own (and rejects a caller-supplied
    # one), and the worker uploads that generated password to the case prefix in S3 for restore.
    s3_store.put_json(client, bucket, "{}/{}".format(batch, s3_store.BATCH_CONFIG_NAME), {
        "eval_name": eval_name, "created_at": stamp, "num_turns": num_turns,
        "compute": compute, "mngr_branch": mngr_branch, "fct_repo": fct_repo, "fct_branch": fct_branch,
        "cases": cases,
    })

    CLONES_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_base(fct_repo, fct_branch)

    results = []
    for index, case in enumerate(cases, 1):
        case_pref = s3_store.case_prefix(batch, eval_name, case["id"])
        host_name = "EVAL-{}-CASE-{}".format(eval_name, case["id"])
        case_config = {
            "eval_name": eval_name, "case_name": case["id"], "persona": case["persona"],
            "first_prompt": case["first_prompt"], "num_turns": num_turns,
            "s3_bucket": bucket, "s3_prefix": case_pref,
        }
        print("\n  [{}/{}] {}".format(index, len(cases), host_name), flush=True)
        clone = _prepare_clone(case, case_config)

        backup_env = backup_env_block(env, s3_store.restic_repo_url(env, case_pref))
        status, body = _post_json(
            "{}/api/v1/workspaces".format(_api_base(port)),
            build_create_payload(clone, host_name, anthropic_key, compute, backup_env),
        )
        if status != 202:
            print("     ERR create HTTP {}: {}".format(status, body), flush=True)
            results.append({"case": case["id"], "ok": False, "error": str(body)[:200]})
            continue
        outcome = _await_create(port, body["operation_id"])
        results.append({"case": case["id"], **outcome})
        print("     {}".format("OK agent {}".format(outcome.get("agent_id")) if outcome["ok"]
                               else "ERR {}".format(outcome.get("error"))), flush=True)

    ok = sum(1 for r in results if r.get("ok"))
    print("\n" + "=" * 66, flush=True)
    print("  {}/{} workspaces launched. They self-complete; results land in S3.".format(ok, len(results)), flush=True)
    print("  inspect:  minds-evals inspect {}".format(batch), flush=True)
    print("=" * 66, flush=True)
    if ok == 0:
        sys.exit(1)
    return {"batch": batch, "results": results}
