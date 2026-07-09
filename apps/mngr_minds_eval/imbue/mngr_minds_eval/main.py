"""mngr_minds_eval -- Minds eval harness (CLI), run inside the minds-box container.

Subcommands:
  prepare-test-clones  Clone the FCT branch once per (persona x trial) and slot each persona's
                       config into scripts/first_command.json, committed.
  launch-workspaces    Create a Modal workspace for every prepared clone -- automating the
                       create form (Modal compute, API_KEY provider, backup configure-later,
                       empty branch). Workspace name = EVAL-<eval-set>-CASE-<persona>.
  self-check           Run the offline asserts and exit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_FCT_REPO = "https://github.com/imbue-ai/forever-claude-template.git"
DEFAULT_FCT_BRANCH = "minds-eval-autosend"
DEFAULT_CLONES_DIR = Path("/work/clones")
DEFAULT_BASE_DIR = Path("/work/eval-base")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_personas_from_obj(obj: object) -> list[dict]:
    personas = obj["personas"] if isinstance(obj, dict) else obj
    if not isinstance(personas, list) or not personas:
        raise ValueError('personas file must be a non-empty list (or {"personas": [...]})')
    out = []
    for i, p in enumerate(personas):
        pid = slugify(str(p.get("id") or "persona-{}".format(i + 1)))
        prompt = str(p.get("first_prompt", "")).strip()
        if not prompt:
            raise ValueError("persona {!r} is missing first_prompt".format(pid))
        out.append({"id": pid, "persona": str(p.get("persona", "")).strip(), "first_prompt": prompt})
    return out


def expand(personas: list[dict], trials: int) -> list[dict]:
    tasks = []
    for p in personas:
        for t in range(1, trials + 1):
            cid = p["id"] if trials == 1 else "{}-{}".format(p["id"], t)
            tasks.append({**p, "id": cid})
    return tasks


def _sh(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


# --- prepare-test-clones ---------------------------------------------------------------------


def ensure_base(repo: str, branch: str, base_dir: Path) -> None:
    # Fresh clone each run so the base always matches the requested branch (never a stale one).
    if base_dir.exists():
        shutil.rmtree(base_dir)
    print(">> cloning base {}@{} -> {}".format(repo, branch, base_dir))
    _sh("git", "clone", "--branch", branch, repo, str(base_dir))


def prepare_one(config: dict, clones_dir: Path, base_dir: Path) -> Path:
    """Local-clone the base, slot the persona config, commit. Only committed content ships."""
    cid = config["id"]
    clone = clones_dir / cid
    if clone.exists():
        shutil.rmtree(clone)
    _sh("git", "clone", str(base_dir), str(clone))
    (clone / "scripts" / "first_command.json").write_text(json.dumps(config, indent=2))
    _sh("git", "-C", str(clone), "add", "-A")
    _sh("git", "-C", str(clone), "-c", "user.email=eval@minds", "-c", "user.name=minds-eval",
        "commit", "-q", "-m", "slot testcase {}".format(cid))
    return clone


def prepare_test_clones(
    personas_path: str,
    *,
    repo: str,
    branch: str,
    trials: int,
    clones_dir: Path,
    base_dir: Path,
) -> list[Path]:
    personas = load_personas_from_obj(json.loads(Path(personas_path).read_text()))
    tasks = expand(personas, trials)
    clones_dir.mkdir(parents=True, exist_ok=True)
    ensure_base(repo, branch, base_dir)
    print(">> preparing {} clone(s): {} persona x {} trial ...".format(len(tasks), len(personas), trials))
    clones = []
    for config in tasks:
        clone = prepare_one(config, clones_dir, base_dir)
        clones.append(clone)
        print("  [OK] {}: {}".format(config["id"], clone))
    print(">> done: {} clone(s) ready under {}".format(len(clones), clones_dir))
    return clones


# --- launch-workspaces -----------------------------------------------------------------------


def _api_base() -> str:
    return "http://127.0.0.1:{}".format(os.environ.get("MINDS_BARE_PORT", "8420"))


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:500]}


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode())


def workspace_name(eval_set: str, persona: str) -> str:
    return "EVAL-{}-CASE-{}".format(eval_set, persona)


def build_launch_payload(clone_path: Path, name: str, api_key: str) -> dict:
    """The create-form POST body, field-for-field:

    Modal compute, API_KEY provider (+ the env-supplied key), backup configure-later, and an
    EMPTY branch -- a local clone is already on the right commit, and passing a branch trips
    mngr's checkout_branch(FETCH_HEAD) on the use-in-place path (fatal: 'FETCH_HEAD' is not a
    commit). Enum values are UPPERCASE on the wire (the UI just displays them lowercased).
    """
    return {
        "git_url": str(clone_path),
        "host_name": name,
        "branch": "",
        "launch_mode": "MODAL",
        "ai_provider": "API_KEY",
        "anthropic_api_key": api_key,
        "backup_provider": "CONFIGURE_LATER",
    }


def list_prepared_clones(clones_dir: Path) -> list[Path]:
    if not clones_dir.is_dir():
        return []
    return sorted((p for p in clones_dir.iterdir() if (p / ".git").exists()), key=lambda p: p.name)


def launch_one(clone_path: Path, eval_set: str, api_key: str, poll_timeout: float = 900.0) -> dict:
    persona = clone_path.name
    name = workspace_name(eval_set, persona)
    status, body = _post_json("{}/api/v1/workspaces".format(_api_base()), build_launch_payload(clone_path, name, api_key))
    if status != 202:
        return {"persona": persona, "name": name, "ok": False, "error": "create HTTP {}: {}".format(status, body)}
    operation_id = body["operation_id"]

    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        try:
            info = _get_json("{}/api/v1/workspaces/operations/create/{}".format(_api_base(), operation_id))
        except (urllib.error.URLError, OSError):
            time.sleep(5)
            continue
        if info.get("is_done"):
            return {"persona": persona, "name": name, "ok": True, "agent_id": info.get("agent_id")}
        if info.get("error"):
            return {"persona": persona, "name": name, "ok": False, "error": info.get("error")}
        time.sleep(5)
    return {"persona": persona, "name": name, "ok": False, "error": "timed out after {}s".format(int(poll_timeout))}


def launch_workspaces(eval_set: str, *, clones_dir: Path, api_key: str) -> list[dict]:
    clones = list_prepared_clones(clones_dir)
    if not clones:
        raise SystemExit("no prepared clones under {} -- run prepare-test-clones first".format(clones_dir))
    print(">> launching {} workspace(s) for eval set {!r} ...".format(len(clones), eval_set))
    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(clones))) as pool:
        futures = [pool.submit(launch_one, c, eval_set, api_key) for c in clones]
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            print("  [{}] {}: {}".format("OK " if r["ok"] else "ERR", r["name"], r.get("agent_id") or r.get("error")))
    ok = sum(1 for r in results if r["ok"])
    print(">> done: {}/{} workspaces launched -- all in the dashboard.".format(ok, len(results)))
    return results


# --- self-check + CLI ------------------------------------------------------------------------


def self_check() -> None:
    assert slugify("A B!") == "a-b"
    assert load_personas_from_obj([{"id": "A B", "first_prompt": "hi"}])[0]["id"] == "a-b"
    assert load_personas_from_obj({"personas": [{"id": "x", "first_prompt": "y"}]})[0]["first_prompt"] == "y"
    try:
        load_personas_from_obj([{"id": "c", "first_prompt": "  "}])
        raise AssertionError("expected ValueError on empty first_prompt")
    except ValueError:
        pass
    assert [t["id"] for t in expand([{"id": "a", "persona": "p", "first_prompt": "x"}], 1)] == ["a"]
    assert [t["id"] for t in expand([{"id": "a", "persona": "p", "first_prompt": "x"}], 3)] == ["a-1", "a-2", "a-3"]

    assert workspace_name("smoke", "algorithms-student") == "EVAL-smoke-CASE-algorithms-student"
    pay = build_launch_payload(Path("/work/clones/algorithms-student"), "EVAL-smoke-CASE-algorithms-student", "sk-ant-K")
    assert pay["branch"] == "" and pay["launch_mode"] == "MODAL", pay
    assert pay["ai_provider"] == "API_KEY" and pay["backup_provider"] == "CONFIGURE_LATER", pay
    assert pay["git_url"] == "/work/clones/algorithms-student", pay
    assert pay["host_name"] == "EVAL-smoke-CASE-algorithms-student" and pay["anthropic_api_key"] == "sk-ant-K", pay
    print("self-check OK")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mngr-minds-eval", description="Minds eval harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare-test-clones", help="clone the FCT branch per persona x trial and slot each config")
    p.add_argument("personas", help="path to personas.json ([{id,persona,first_prompt}, ...])")
    p.add_argument("--fct-repo", default=DEFAULT_FCT_REPO, help="FCT git URL to clone from")
    p.add_argument("--fct-branch", default=DEFAULT_FCT_BRANCH, help="FCT branch to base each clone off")
    p.add_argument("-n", "--trials", type=int, default=1, help="clones to prepare per persona")
    p.add_argument("--clones-dir", type=Path, default=DEFAULT_CLONES_DIR)
    p.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)

    lw = sub.add_parser("launch-workspaces", help="create a Modal workspace for every prepared clone")
    lw.add_argument("--eval-set", required=True, help="eval set name; workspace = EVAL-<set>-CASE-<persona>")
    lw.add_argument("--clones-dir", type=Path, default=DEFAULT_CLONES_DIR)
    lw.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY", ""),
                    help="Anthropic API key (defaults to $ANTHROPIC_API_KEY)")

    sub.add_parser("self-check", help="run offline asserts and exit")

    args = parser.parse_args()
    if args.command == "self-check":
        self_check()
        return
    if args.command == "prepare-test-clones":
        if args.trials < 1:
            parser.error("--trials must be >= 1")
        prepare_test_clones(
            args.personas,
            repo=args.fct_repo,
            branch=args.fct_branch,
            trials=args.trials,
            clones_dir=args.clones_dir,
            base_dir=args.base_dir,
        )
        return
    if args.command == "launch-workspaces":
        if not args.api_key:
            parser.error("set ANTHROPIC_API_KEY (or --api-key) -- workspaces launch with ai_provider=API_KEY")
        launch_workspaces(args.eval_set, clones_dir=args.clones_dir, api_key=args.api_key)


if __name__ == "__main__":
    main()
