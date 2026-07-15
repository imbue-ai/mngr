"""Launch an eval batch: prepare one FCT clone per case, then create one workspace per case.

Each case's clone carries a scripts/test_case_metadata.json with the S3 target, the case's restic repo +
password, and the scoped AWS creds; backup_provider is configure_later and the in-sandbox worker
drives restic itself. The run self-completes and everything is retrievable from S3.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from imbue.mngr_minds_eval import box as box_mod
from imbue.mngr_minds_eval import minds_client
from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import workspace

# The forever-claude-template (workspace template) each eval case is cloned from. The default
# branch carries the eval worker (eval_responder + config.json gating); a branch WITHOUT it won't
# auto-run the conversation or snapshot. Override with --fct-repo / --fct-branch.
DEFAULT_FCT_REPO = "https://github.com/imbue-ai/default-workspace-template.git"
DEFAULT_FCT_BRANCH = "minds-eval-autosend"
CLONES_DIR = Path("/work/clones")
BASE_DIR = Path("/work/eval-base")
BOX_MNGR = Path("/work/mngr")

_VENDOR_EXCLUDES = (
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "*.egg-info",
    ".coverage",
)


def _sh(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


class _Live:
    """Compact per-case status table that redraws in place (one line per case), so parallel progress
    stays a fixed-height block instead of scrolling. Plain lines when stdout is not a tty."""

    def __init__(self, case_ids: list[str]):
        self._rows = {cid: "queued" for cid in case_ids}
        self._lock = threading.Lock()
        self._tty = sys.stdout.isatty()
        self._drawn = 0

    def set(self, case_id: str, status: str) -> None:
        with self._lock:
            self._rows[case_id] = status
            if not self._tty:
                print("  {:<24} {}".format(case_id[:24], status), flush=True)
                return
            if self._drawn:
                sys.stdout.write("\033[{}A".format(self._drawn))
            for cid, st in self._rows.items():
                sys.stdout.write("\033[K  {:<24} {}\n".format(cid[:24], st))
            self._drawn = len(self._rows)
            sys.stdout.flush()


# create/poll and the HTTP helpers live in minds_client (shared with workspace).
_api_base = minds_client.api_base
_post_json = minds_client.post_json
_get_json = minds_client.get_json


# A case's prompts are sent one per turn. A literal string is sent verbatim; this sentinel makes the
# in-sandbox worker role-play the client instead -- it feeds (transcript-so-far + persona) to the
# Anthropic API and sends back a short casual reply. The first prompt cannot be the sentinel (there
# is no transcript to decide from yet).
DECIDE_SENTINEL = "DECIDE_FROM_PERSONA"


def derive_case_id(case: dict, index: int) -> str:
    """A case's stable id: its explicit 'id', else a positional 'case-N'. Launch writes results under
    this id and status/evaluate read under it, so the two sides must derive it identically."""
    return str(case.get("id") or "case-{}".format(index + 1))


def normalize_cases(personas: object) -> list[dict]:
    if not isinstance(personas, list) or not personas:
        raise ValueError("'personas' must be a non-empty list")
    out = []
    for index, raw_case in enumerate(personas):
        case: Any = raw_case
        if not isinstance(case, dict):
            raise ValueError("each persona case must be an object")
        case_id = derive_case_id(case, index)
        raw_prompts = case.get("prompts")
        if not isinstance(raw_prompts, list) or not raw_prompts:
            raise ValueError("case {!r} must have a non-empty 'prompts' list".format(case_id))
        prompts = [str(p).strip() for p in raw_prompts]
        if any(not p for p in prompts):
            raise ValueError("case {!r} has an empty prompt".format(case_id))
        if prompts[0] == DECIDE_SENTINEL:
            raise ValueError(
                "case {!r}: the first prompt cannot be {} (nothing to decide from yet)".format(
                    case_id, DECIDE_SENTINEL
                )
            )
        out.append({"id": case_id, "persona": str(case.get("persona", "")).strip(), "prompts": prompts})
    ids = [str(c["id"]) for c in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        # Two cases with the same id collide on one S3 prefix and one Modal host name (one silently
        # overwrites/destroys the other), so reject it up front.
        raise ValueError("duplicate case id(s): {}".format(", ".join(dupes)))
    return out


def load_config(config_path: Path) -> dict:
    """Read + validate the eval config json. This exact object is stored verbatim in S3 as the batch
    config (plus created_at / restic_password / mngr_sha added at launch). Each case's 'prompts' array
    defines that case's turns, so different cases can run different numbers of turns."""
    if not config_path.is_file():
        raise SystemExit("no such config file: {}".format(config_path))
    config = json.loads(config_path.read_text())
    for key in ("name", "mngr_branch", "personas"):
        if not config.get(key):
            raise SystemExit("eval config is missing required key: {!r}".format(key))
    # The name IS the batch identity: the S3 prefix and the Modal env (minds-staging-<name>) both
    # key on it, and launch preflights that neither exists yet. Require it to already be a valid
    # Modal user_id (lowercase alnum + dashes, <=40) so no sanitization can alias two names.
    name = str(config["name"])
    if name != box_mod.sanitize_user_id(name) or len(name) > 40:
        raise SystemExit(
            "eval config: 'name' must be lowercase letters/digits/dashes, at most 40 chars "
            "(got {!r}) -- it names the batch's S3 prefix and Modal env".format(name)
        )
    try:
        normalize_cases(config["personas"])  # validate case shape now, on the host
    except ValueError as exc:
        raise SystemExit("eval config: {}".format(exc)) from exc
    return config


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
    (clone / "scripts" / "test_case_metadata.json").write_text(json.dumps(case_config, indent=2))
    _sh("git", "-C", str(clone), "add", "-A")
    _sh(
        "git",
        "-C",
        str(clone),
        "-c",
        "user.email=eval@minds",
        "-c",
        "user.name=minds-eval",
        "commit",
        "-q",
        "-m",
        "eval case {}".format(case["id"]),
    )
    return clone


def _list_workspaces(port: str) -> list[dict]:
    listing = _get_json("{}/api/v1/workspaces".format(_api_base(port)))
    return [w for w in listing.get("workspaces", []) if w.get("agent_id")]


def _destroy_and_wait(port: str, agent_id: str, label: str, timeout: float = 600.0, quiet: bool = False) -> bool:
    """POST destroy and poll until done. Returns True on confirmed teardown. Each destroy removes the
    Modal sandbox AND its host record from the environment, so the host name frees up."""

    def _say(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    status, body = _post_json("{}/api/v1/workspaces/{}/destroy".format(_api_base(port), agent_id), {})
    if status != 202:
        _say("  [ERR ] {}: {}".format(label, str(body)[:150]))
        return False
    operation_id = body.get("operation_id", agent_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = _get_json("{}/api/v1/workspaces/operations/destroy/{}".format(_api_base(port), operation_id))
        except (urllib.error.URLError, OSError):
            time.sleep(4)
            continue
        if info.get("is_done"):
            _say("  [OK  ] destroyed {}".format(label))
            return True
        time.sleep(4)
    _say("  [WARN] {} did not confirm destroy in time".format(label))
    return False


def destroy_existing_workspace(port: str, host_name: str, quiet: bool = False) -> None:
    """Idempotent create: if a workspace with this host name already exists (a re-run with the same
    name, or an interrupted prior run), destroy it first -- the name is registered in the Modal
    environment and survives box restarts, so only an actual destroy clears it."""
    try:
        existing = _list_workspaces(port)
    except (urllib.error.URLError, OSError):
        return
    match = next((w for w in existing if (w.get("name") or "").lower() == host_name.lower()), None)
    if match is not None:
        _destroy_and_wait(port, match["agent_id"], host_name, quiet=quiet)


def launch_batch(*, config: dict, anthropic_key: str, port: str) -> dict:
    env = s3_store.load_aws_env()
    client = s3_store.make_client(env)
    bucket = env["MINDS_EVAL_BUCKET"]

    eval_name = config["name"]
    fct_repo = config.get("fct_repo", DEFAULT_FCT_REPO)
    fct_branch = config.get("fct_branch", DEFAULT_FCT_BRANCH)
    cases = normalize_cases(config["personas"])
    # The name IS the batch: S3 prefix and Modal env both key on it (uniqueness preflighted on the
    # host before this runs).
    batch = eval_name

    print("=" * 66, flush=True)
    print("  EVAL BATCH  {}".format(batch), flush=True)
    print("  cases: {}   bucket: {}".format(len(cases), bucket), flush=True)
    print("=" * 66, flush=True)

    # Store the user's config verbatim + the fields launch adds: created_at, the batch restic
    # password (we own it -- we drive restic ourselves), the exact mngr SHA this box is built at
    # (stamped into the box env), and the batch's own Modal env (user_id) -- together these let
    # `visit-batch` rebuild the exact computer this batch ran on and see exactly its workspaces.
    restic_password = secrets.token_urlsafe(24)
    user_id = box_mod.sanitize_user_id(batch)
    s3_store.put_json(
        client,
        bucket,
        "{}/{}".format(batch, s3_store.BATCH_CONFIG_NAME),
        {
            **config,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "restic_password": restic_password,
            "mngr_sha": os.environ.get("MINDS_BOX_MNGR_REF", ""),
            "modal_user_id": user_id,
            "modal_env": box_mod.modal_env_name(user_id),
        },
    )

    CLONES_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_base(fct_repo, fct_branch)

    # Prepare every clone first (git clone + vendor mngr + slot test_case_metadata.json). Local and
    # fast; kept serial for simple output. Everything the in-sandbox worker needs is in the metadata
    # file (S3 target, restic repo/password, scoped AWS creds) -- so the worker doesn't depend on
    # minds' backup provisioning (which doesn't land a restic.env in the sandbox).
    prepared = []
    for case in cases:
        case_pref = s3_store.case_prefix(batch, eval_name, case["id"])
        case_config = {
            "eval_name": eval_name,
            "case_name": case["id"],
            "persona": case["persona"],
            "prompts": case["prompts"],  # one per turn; a literal is sent verbatim, DECIDE_FROM_PERSONA is role-played
            # per-case wall-clock budget (default 1h); past it the in-sandbox worker marks the run timed_out
            "timeout_seconds": config.get("timeout_seconds", 3600),
            "s3_bucket": bucket,
            "s3_prefix": case_pref,
            "restic_repository": s3_store.restic_repo_url(env, case_pref),
            "restic_password": restic_password,
            "aws_access_key_id": env["AWS_ACCESS_KEY_ID"],
            "aws_secret_access_key": env["AWS_SECRET_ACCESS_KEY"],
            "aws_region": env.get("AWS_DEFAULT_REGION", "us-east-1"),
            "anthropic_api_key": anthropic_key,  # so the worker can role-play the client on DECIDE_FROM_PERSONA turns
        }
        print("  preparing clone: {}".format(case["id"]), flush=True)
        prepared.append((case, _prepare_clone(case, case_config)))

    live = _Live([case["id"] for case, _ in prepared])

    def _create(case: dict, clone: Path) -> dict:
        cid = case["id"]
        host_name = "EVAL-{}-CASE-{}".format(eval_name, cid)
        live.set(cid, "clearing old name")
        destroy_existing_workspace(port, host_name, quiet=True)  # idempotent: re-run with same name works
        try:
            agent_id = workspace.create_workspace(
                port=port,
                fct_link=str(clone),
                name=host_name,
                ai_provider="api_key",
                anthropic_key=anthropic_key,
                backup_provider="configure_later",
                on_stage=lambda s: live.set(cid, s),
            )
            live.set(cid, "OK -- agent {}".format(agent_id))
            return {"case": cid, "ok": True, "agent_id": agent_id}
        except minds_client.CreateError as exc:
            live.set(cid, "ERR -- {}".format(str(exc)[:60]))
            return {"case": cid, "ok": False, "error": str(exc)}

    # Concurrent creates race ONLY on the one-time creation of the Modal env. If the env already has
    # a workspace it exists, so we fan out all at once. Otherwise prime one solo (that create makes
    # the env), then fan the rest.
    try:
        env_exists = len(_list_workspaces(port)) > 0
    except (urllib.error.URLError, OSError):
        env_exists = False
    to_create = list(prepared)
    results = []
    if not env_exists:
        print("\n>> new Modal env -- priming 1 workspace, then fanning:", flush=True)
        results.append(_create(*to_create.pop(0)))
    else:
        print("\n>> creating {} workspace(s) in parallel:".format(len(to_create)), flush=True)
    if to_create:
        with ThreadPoolExecutor(max_workers=min(8, len(to_create))) as pool:
            results.extend(pool.map(lambda pair: _create(*pair), to_create))

    ok = sum(1 for r in results if r.get("ok"))
    print("\n" + "=" * 66, flush=True)
    print("  {}/{} workspaces launched. They self-complete; results land in S3.".format(ok, len(results)), flush=True)
    print("  inspect:  minds-evals inspect {}".format(batch), flush=True)
    print("=" * 66, flush=True)
    if ok == 0:
        sys.exit(1)
    return {"batch": batch, "results": results}
