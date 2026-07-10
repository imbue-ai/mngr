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
import sys
import time
import urllib.error
import urllib.request
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


_VENDOR_EXCLUDES = (".git", ".venv", "node_modules", "__pycache__", "*.pyc", ".pytest_cache",
                    ".mypy_cache", ".ruff_cache", "dist", "build", "*.egg-info", ".coverage")


def _vendor_mngr_into(mngr_src: Path, clone: Path) -> None:
    """Overlay the box's mngr checkout onto the clone's vendor/mngr so the sandbox runs THAT mngr.
    rsync the source (minus venv/build/caches) over vendor/mngr; the later commit ships it."""
    dest = clone / "vendor" / "mngr"
    dest.mkdir(parents=True, exist_ok=True)
    args = ["rsync", "-a", "--delete"]
    for pattern in _VENDOR_EXCLUDES:
        args += ["--exclude", pattern]
    args += [str(mngr_src).rstrip("/") + "/", str(dest).rstrip("/") + "/"]
    _sh(*args)


def prepare_one(config: dict, clones_dir: Path, base_dir: Path, vendor_mngr: Path | None = None) -> Path:
    """Local-clone the base, optionally vendor mngr, slot the persona config, commit."""
    cid = config["id"]
    clone = clones_dir / cid
    if clone.exists():
        shutil.rmtree(clone)
    _sh("git", "clone", str(base_dir), str(clone))
    if vendor_mngr is not None:
        _vendor_mngr_into(vendor_mngr, clone)
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
    vendor_mngr: Path | None = None,
) -> list[Path]:
    personas = load_personas_from_obj(json.loads(Path(personas_path).read_text()))
    tasks = expand(personas, trials)
    clones_dir.mkdir(parents=True, exist_ok=True)
    ensure_base(repo, branch, base_dir)  # fresh clone of the branch tip every run (rm + git clone)
    if vendor_mngr is not None:
        print(">> vendoring mngr from {} into each clone's vendor/mngr".format(vendor_mngr))
    print(">> preparing {} clone(s): {} persona x {} trial ...".format(len(tasks), len(personas), trials))
    clones = []
    for config in tasks:
        clone = prepare_one(config, clones_dir, base_dir, vendor_mngr=vendor_mngr)
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


def _start_create(clone_path: Path, eval_set: str, api_key: str) -> dict:
    """POST the create (returns immediately); the server provisions in the background."""
    name = workspace_name(eval_set, clone_path.name)
    status, body = _post_json("{}/api/v1/workspaces".format(_api_base()), build_launch_payload(clone_path, name, api_key))
    if status != 202:
        return {"name": name, "op": None, "error": "create HTTP {}: {}".format(status, body)}
    return {"name": name, "op": body.get("operation_id"), "error": None}


def _poll_op(operation_id: str) -> dict:
    try:
        return _get_json("{}/api/v1/workspaces/operations/create/{}".format(_api_base(), operation_id))
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def _fmt_elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return "{}m{:02d}s".format(minutes, secs) if minutes else "{}s".format(secs)


def _term_width() -> int:
    return max(48, shutil.get_terminal_size((100, 24)).columns - 1)


_SPINNER = "|/-\\"


def _status_row(final: dict | None, name: str, stage: str, elapsed: str, spin: str, width: int) -> str:
    if final is None:
        icon, detail = spin, stage
    elif final["ok"]:
        icon, detail = "OK", "ready: {}".format(final.get("agent_id"))
    else:
        icon, detail = "XX", "FAILED: {}".format(final.get("error", ""))
    return "  {:<2} {:<34} {:<30} {:>7}".format(icon, name[:34], detail[:30], elapsed)[:width]


class _Live:
    """Redraw a block of lines in place on a TTY; plain per-line logging otherwise."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._lines = 0

    def update(self, lines: list[str]) -> None:
        if not self.enabled:
            return
        if self._lines:
            sys.stdout.write("\033[{}A".format(self._lines))
        sys.stdout.write("".join("\033[K" + line + "\n" for line in lines))
        sys.stdout.flush()
        self._lines = len(lines)

    def seal(self) -> None:
        self._lines = 0  # commit the current block so the next update() starts below it


def _drive(started: list[dict], live: _Live, title: str, timeout: float = 1200.0) -> list[dict]:
    """Poll the started creates to completion, live-rendering a table (TTY) or logging lines."""
    starts = {s["name"]: time.time() for s in started}
    finals: dict[str, dict] = {}
    width = _term_width()
    tick = 0
    deadline = time.time() + timeout
    while True:
        rows, newly = [], []
        for s in started:
            name = s["name"]
            elapsed = _fmt_elapsed(time.time() - starts[name])
            if name in finals:
                rows.append(_status_row(finals[name], name, "", elapsed, "", width))
                continue
            if s["op"] is None:
                finals[name] = {"name": name, "ok": False, "error": s["error"]}
                newly.append(finals[name])
                rows.append(_status_row(finals[name], name, "", elapsed, "", width))
                continue
            info = _poll_op(s["op"])
            if info.get("is_done"):
                finals[name] = {"name": name, "ok": True, "agent_id": info.get("agent_id")}
                newly.append(finals[name])
                rows.append(_status_row(finals[name], name, "", elapsed, "", width))
            elif info.get("error"):
                finals[name] = {"name": name, "ok": False, "error": info.get("error")}
                newly.append(finals[name])
                rows.append(_status_row(finals[name], name, "", elapsed, "", width))
            else:
                stage = info.get("status_text") or info.get("status") or "working..."
                rows.append(_status_row(None, name, stage, elapsed, _SPINNER[tick % len(_SPINNER)], width))
        ready = sum(1 for f in finals.values() if f["ok"])
        header = "{}  (ready {}, working {}, failed {})".format(
            title, ready, len(started) - len(finals), len(finals) - ready)[:width]
        if live.enabled:
            live.update([header] + rows)
        else:
            for f in newly:
                tail = f.get("agent_id") if f["ok"] else f.get("error")
                print("  [{}] {} : {}".format("OK " if f["ok"] else "ERR", f["name"], tail), flush=True)
        if len(finals) == len(started) or time.time() > deadline:
            break
        tick += 1
        time.sleep(2.0)
    for s in started:
        finals.setdefault(s["name"], {"name": s["name"], "ok": False, "error": "timed out"})
    live.seal()
    return [finals[s["name"]] for s in started]


def launch_workspaces(eval_set: str, *, clones_dir: Path, api_key: str) -> list[dict]:
    clones = list_prepared_clones(clones_dir)
    if not clones:
        raise SystemExit("no prepared clones under {} -- run prepare-test-clones first".format(clones_dir))
    total = len(clones)
    live = _Live(sys.stdout.isatty())
    rule = "=" * min(_term_width(), 66)

    print(rule, flush=True)
    print("  LAUNCHING {} WORKSPACE(S)   eval set: {}".format(total, eval_set), flush=True)
    print("  compute: Modal    ai: api_key    (the shared Modal env is primed first)", flush=True)
    print(rule, flush=True)

    # STEP 1 -- prime the shared Modal environment with the first create (solo). Concurrent creates
    # only race on this one-time env creation; once it exists, the rest are parallel-safe.
    first = clones[0]
    print("\n>> STEP 1/2  PRIME -- first workspace solo, creates the shared Modal environment:", flush=True)
    primed = _drive([_start_create(first, eval_set, api_key)], live, "  priming")[0]
    results = [primed]

    # STEP 2 -- the rest, in parallel (env now exists). Serial fallback if the prime failed.
    rest = clones[1:]
    if rest and not primed["ok"]:
        print("\n>> STEP 2/2  prime FAILED -- env may be missing; creating the rest ONE AT A TIME:", flush=True)
        for clone in rest:
            results.extend(_drive([_start_create(clone, eval_set, api_key)], live, "  serial"))
    elif rest:
        print("\n>> STEP 2/2  PARALLEL -- launching the remaining {} at once:".format(len(rest)), flush=True)
        started = [_start_create(c, eval_set, api_key) for c in rest]
        results.extend(_drive(started, live, "  launching"))

    ok = sum(1 for r in results if r["ok"])
    print("\n" + rule, flush=True)
    print("  RESULT: {}/{} workspaces up   eval set: {}".format(ok, total, eval_set), flush=True)
    for r in results:
        tail = "agent {}".format(r.get("agent_id")) if r["ok"] else str(r.get("error"))[:60]
        print("  [{}] {:<40} {}".format("OK " if r["ok"] else "ERR", r["name"][:40], tail), flush=True)
    print(rule, flush=True)
    return results


# --- retrieve-test-results -------------------------------------------------------------------

# mngr `list`/`rsync`/`transcript` probe every enabled provider; only Modal works in the box, so
# a single unreachable provider errors the whole call. Disable the rest for our mngr subprocesses.
_NON_MODAL_PROVIDERS = ("DOCKER", "AZURE", "AWS", "VULTR", "LIMA", "IMBUE_CLOUD", "GCP", "OVH")
# The in-sandbox chat_watcher writes this (under MNGR_HOST_DIR, above the agent's repo).
_EVAL_STATE_REMOTE_PATH = "/mngr/eval_state.json"
# The workspace's chat (primary) agent id -- the transcript we want, NOT the system-services agent
# the workspace's API agent_id resolves to.
_CHAT_AGENT_ID_REMOTE_PATH = "/mngr/initial_chat_agent_id"
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


def _rsync_out(agent_id: str, remote_path: str, env: dict, dest_dir: Path):
    """rsync a remote file out into dest_dir (mngr rsync syncs INTO a dir). Returns (proc, file|None)."""
    shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    proc = _run_mngr(["rsync", "{}:{}".format(agent_id, remote_path), str(dest_dir) + "/"], env)
    local = dest_dir / Path(remote_path).name
    return proc, (local if local.is_file() else None)


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

        # 1. read the state file (mngr rsync syncs INTO a directory, so use a scratch dir).
        state_dir = out_dir / ".state-{}".format(agent_id)
        proc, state_file = _rsync_out(agent_id, _EVAL_STATE_REMOTE_PATH, env, state_dir)

        if state_file is None:
            kind = _classify_error(proc.stderr or proc.stdout) if proc.returncode != 0 else "absent"
            shutil.rmtree(state_dir, ignore_errors=True)
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
            state = json.loads(state_file.read_text())
        except (ValueError, OSError) as exc:
            record["status"] = "error"
            record["error"] = str(exc)
            print("  [ERR ] {}: unreadable state file ({})".format(persona, exc), flush=True)
            results.append(record)
            continue
        finally:
            shutil.rmtree(state_dir, ignore_errors=True)

        waits = state.get("waits_processed_count")
        record["waits_processed_count"] = waits
        if state.get("test_state") != "finished":
            record["status"] = "ongoing"
            print("  [ONGO] {}: ongoing, {} wait(s) processed".format(persona, waits), flush=True)
            results.append(record)
            continue

        # 2. finished -> pull the Claude transcript. The workspace's API agent_id resolves to the
        #    host's system-services agent (no transcript); the CHAT agent id lives in the sandbox.
        chat_dir = out_dir / ".chatid-{}".format(agent_id)
        _cproc, chat_id_file = _rsync_out(agent_id, _CHAT_AGENT_ID_REMOTE_PATH, env, chat_dir)
        chat_agent_id = chat_id_file.read_text().strip() if chat_id_file else agent_id
        shutil.rmtree(chat_dir, ignore_errors=True)

        stem = persona if persona not in seen else "{}.{}".format(persona, agent_id[:8])
        seen.add(persona)
        transcript_path = out_dir / "{}.jsonl".format(stem)
        tproc = _run_mngr(["transcript", chat_agent_id, "--format", "jsonl"], env)
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
    assert _fmt_elapsed(9) == "9s" and _fmt_elapsed(75) == "1m15s"
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
    p.add_argument("--vendor-mngr", type=Path, default=None,
                   help="rsync this mngr checkout into each clone's vendor/mngr (sandbox runs THAT mngr)")

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
            vendor_mngr=args.vendor_mngr,
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
