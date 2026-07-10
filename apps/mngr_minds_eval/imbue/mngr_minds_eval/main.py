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


def _result_detail(r: dict) -> str:
    return "OK {}".format(r.get("agent_id")) if r["ok"] else "ERR {}".format(r.get("error"))


def launch_workspaces(eval_set: str, *, clones_dir: Path, api_key: str) -> list[dict]:
    clones = list_prepared_clones(clones_dir)
    if not clones:
        raise SystemExit("no prepared clones under {} -- run prepare-test-clones first".format(clones_dir))
    # `mngr create` is parallel-safe EXCEPT the one-time creation of the shared Modal environment
    # (minds-staging-<hash>, one per instance): from a cold env, concurrent creates all try to make
    # it and all but one die ("environment with the same name already exists"). So PRIME the env
    # with the first create (serial), then fan the rest out in parallel -- wall-clock ~= 2x one
    # create regardless of N. If priming fails the env may be absent, so fall back to serial.
    total = len(clones)
    print(">> launching {} workspace(s) for eval set {!r}".format(total, eval_set), flush=True)

    first, rest = clones[0], clones[1:]
    first_name = workspace_name(eval_set, first.name)
    print("  [prime 1/{}] {} ... (creates the shared Modal environment)".format(total, first_name), flush=True)
    primed = launch_one(first, eval_set, api_key)
    print("  [prime 1/{}] {} -> {}".format(total, first_name, _result_detail(primed)), flush=True)
    results = [primed]

    if rest and not primed["ok"]:
        print("  !! prime create failed -- running the rest one at a time to avoid the env race", flush=True)
        for i, clone in enumerate(rest, 2):
            r = launch_one(clone, eval_set, api_key)
            results.append(r)
            print("  [{}/{}] {} -> {}".format(i, total, workspace_name(eval_set, clone.name), _result_detail(r)), flush=True)
    elif rest:
        print("  [2-{}/{}] launching {} more in parallel ...".format(total, total, len(rest)), flush=True)
        with ThreadPoolExecutor(max_workers=min(8, len(rest))) as pool:
            futures = [pool.submit(launch_one, c, eval_set, api_key) for c in rest]
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                print("  [par] {} -> {}".format(r["name"], _result_detail(r)), flush=True)

    ok = sum(1 for r in results if r["ok"])
    print(">> done: {}/{} workspaces launched.".format(ok, len(results)), flush=True)
    return results


# --- retrieve-test-results -------------------------------------------------------------------

# mngr `list`/`rsync`/`transcript` probe every enabled provider; only Modal works in the box, so
# a single unreachable provider errors the whole call. Disable the rest for our mngr subprocesses.
_NON_MODAL_PROVIDERS = ("DOCKER", "AZURE", "AWS", "VULTR", "LIMA", "IMBUE_CLOUD", "GCP", "OVH")
# The in-sandbox chat_watcher writes this (under MNGR_HOST_DIR, above the agent's repo).
_EVAL_STATE_REMOTE_PATH = "/mngr/eval_state.json"
_UNREACHABLE_HINTS = ("connection", "timed out", "timeout", "unreachable", "refused",
                      "could not resolve", "no route", "kex_exchange", "ssh:", "closed by remote")
_ABSENT_HINTS = ("no such file", "link_stat", "failed to open", "change_dir")


def _mngr_env() -> dict:
    env = dict(os.environ)
    for provider in _NON_MODAL_PROVIDERS:
        env["MNGR__PROVIDERS__{}__IS_ENABLED".format(provider)] = "false"
    return env


def _run_mngr(args: list[str], env: dict, timeout: float = 300.0) -> subprocess.CompletedProcess:
    # Use mngr's SFTP/rsync-backed transport (rsync, transcript) -- NOT `mngr exec`, whose
    # paramiko env-prefix returns empty output for file reads.
    return subprocess.run(
        ["uv", "run", "mngr", *args], cwd="/work/mngr", env=env,
        capture_output=True, text=True, timeout=timeout,
    )


def _classify_error(text: str) -> str:
    low = text.lower()
    if any(h in low for h in _ABSENT_HINTS):
        return "absent"
    if any(h in low for h in _UNREACHABLE_HINTS):
        return "unreachable"
    return "error"


def _persona_from_name(name: str, eval_set: str) -> str | None:
    prefix = "EVAL-{}-CASE-".format(eval_set)
    return name[len(prefix):] if name.startswith(prefix) else None


def _list_eval_workspaces(eval_set: str) -> list[tuple[str, str]]:
    data = _get_json("{}/api/v1/workspaces".format(_api_base()))
    out = []
    for w in data.get("workspaces", []):
        persona = _persona_from_name(w.get("name") or "", eval_set)
        if persona and w.get("agent_id"):
            out.append((persona, w["agent_id"]))
    return out


def retrieve_test_results(eval_set: str, out_dir: Path) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = _mngr_env()
    workspaces = _list_eval_workspaces(eval_set)
    if not workspaces:
        print("no workspaces named EVAL-{}-CASE-* -- nothing to retrieve".format(eval_set))
        return []

    print(">> retrieving {} case(s) for eval set {!r} -> {}".format(len(workspaces), eval_set, out_dir), flush=True)
    results = []
    seen: set[str] = set()
    for persona, agent_id in sorted(workspaces):
        record: dict = {"persona": persona, "agent_id": agent_id}

        # 1. read the state file via rsync (reliable file transport).
        state_local = out_dir / ".{}.state.json".format(agent_id)
        proc = _run_mngr(["rsync", "{}:{}".format(agent_id, _EVAL_STATE_REMOTE_PATH), str(state_local)], env)
        if proc.returncode != 0:
            kind = _classify_error(proc.stderr or proc.stdout)
            record["status"] = {"unreachable": "unreachable", "absent": "no_state"}.get(kind, "error")
            if kind == "unreachable":
                print("  [ERR ] {}: machine not accessible".format(persona), flush=True)
            elif kind == "absent":
                print("  [WAIT] {}: no eval_state.json yet (test not started?)".format(persona), flush=True)
            else:
                record["error"] = (proc.stderr or "").strip()[:300]
                print("  [ERR ] {}: {}".format(persona, record["error"]), flush=True)
            results.append(record)
            continue

        try:
            state = json.loads(state_local.read_text())
        except (ValueError, OSError) as exc:
            record["status"] = "error"
            record["error"] = str(exc)
            print("  [ERR ] {}: unreadable state file ({})".format(persona, exc), flush=True)
            results.append(record)
            continue
        finally:
            state_local.unlink(missing_ok=True)

        waits = state.get("waits_processed_count")
        record["waits_processed_count"] = waits
        if state.get("test_state") != "finished":
            record["status"] = "ongoing"
            print("  [ONGO] {}: ongoing, {} wait(s) processed".format(persona, waits), flush=True)
            results.append(record)
            continue

        # 2. finished -> pull the Claude transcript.
        stem = persona if persona not in seen else "{}.{}".format(persona, agent_id[:8])
        seen.add(persona)
        transcript_path = out_dir / "{}.jsonl".format(stem)
        tproc = _run_mngr(["transcript", agent_id, "--format", "jsonl"], env)
        if tproc.returncode != 0:
            record["status"] = "finished_no_transcript"
            record["error"] = (tproc.stderr or "").strip()[:300]
            print("  [ERR ] {}: finished ({} waits) but transcript failed: {}".format(persona, waits, record["error"]), flush=True)
        else:
            transcript_path.write_text(tproc.stdout)
            record["status"] = "finished"
            record["transcript"] = str(transcript_path)
            print("  [DONE] {}: finished ({} waits) -> {}".format(persona, waits, transcript_path.name), flush=True)
        results.append(record)

    (out_dir / "summary.json").write_text(json.dumps({"eval_set": eval_set, "results": results}, indent=2))
    done = sum(1 for r in results if r["status"] == "finished")
    print(">> {} finished / {} total; summary -> {}".format(done, len(results), out_dir / "summary.json"), flush=True)
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

    assert _persona_from_name("EVAL-smoke-CASE-founder", "smoke") == "founder"
    assert _persona_from_name("EVAL-other-CASE-x", "smoke") is None
    assert _classify_error("rsync: link_stat /mngr/eval_state.json failed: No such file or directory") == "absent"
    assert _classify_error("ssh: connect to host h port 22: Connection refused") == "unreachable"
    assert _classify_error("some other failure") == "error"
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

    rt = sub.add_parser("retrieve-test-results", help="pull each case's state + Claude transcript")
    rt.add_argument("--eval-set", required=True, help="eval set name (matches EVAL-<set>-CASE-*)")
    rt.add_argument("-o", "--out-dir", type=Path, required=True, help="directory for transcripts + summary.json")

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
        return
    if args.command == "retrieve-test-results":
        retrieve_test_results(args.eval_set, args.out_dir)
        return


if __name__ == "__main__":
    main()
