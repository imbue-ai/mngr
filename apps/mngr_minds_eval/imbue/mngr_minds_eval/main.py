"""Create one Modal workspace per (persona x trial) from the FCT autosend branch.

Runs INSIDE the minds-box container (the create API is the container-local Minds server and
``git_url`` must be a path Minds itself can read). For each persona it makes a full local clone
of the autosend base, slots the persona's config into ``scripts/first_command.json``, commits it
(only committed content reaches the sandbox via git-mirror), and POSTs the create -- so every
workspace lands in the one dashboard. The in-sandbox chat-watcher (FCT ``minds-eval-autosend``
branch) then delivers each persona's ``first_prompt`` as the user, once its agent goes idle.

Console script: ``mngr-minds-eval <personas.json> [--branch ...] [-n TRIALS]``.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CLONES_DIR = Path("/work/clones")
BASE_DIR = Path("/work/eval-base")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_personas_from_obj(obj: object) -> list[dict]:
    personas = obj["personas"] if isinstance(obj, dict) else obj
    if not isinstance(personas, list) or not personas:
        raise ValueError('persona file must be a non-empty list (or {"personas": [...]})')
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


def build_payload(config: dict, branch: str, api_key: str) -> dict:
    cid = config["id"]
    return {
        "git_url": str(CLONES_DIR / cid),
        "host_name": cid,
        "branch": branch,
        "launch_mode": "MODAL",  # enum value is UPPERCASE (the UI just displays it lowercased)
        "ai_provider": "API_KEY",
        "anthropic_api_key": api_key,
    }


def _sh(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


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


def ensure_base(repo: str, branch: str) -> None:
    # run-eval.sh copies the host-cloned base in as /work/eval-base (so the container needs no
    # git creds); fall back to cloning here when the runner is invoked standalone.
    if (BASE_DIR / ".git").exists():
        print(">> using existing base at {}".format(BASE_DIR))
        return
    print(">> cloning base {}@{}".format(repo, branch))
    _sh("git", "clone", "--branch", branch, repo, str(BASE_DIR))


def create_one(config: dict, branch: str, api_key: str, poll_timeout: float = 900.0) -> dict:
    cid = config["id"]
    clone = CLONES_DIR / cid
    if clone.exists():
        shutil.rmtree(clone)
    try:
        _sh("git", "clone", str(BASE_DIR), str(clone))
        (clone / "scripts" / "first_command.json").write_text(json.dumps(config, indent=2))
        _sh("git", "-C", str(clone), "add", "-A")
        _sh("git", "-C", str(clone), "-c", "user.email=eval@minds", "-c", "user.name=minds-eval",
            "commit", "-q", "-m", "slot testcase {}".format(cid))
    except subprocess.CalledProcessError as exc:
        return {"id": cid, "ok": False, "error": "git: {}".format((exc.stderr or "").strip()[:300])}

    status, body = _post_json("{}/api/v1/workspaces".format(_api_base()), build_payload(config, branch, api_key))
    if status != 202:
        return {"id": cid, "ok": False, "error": "create HTTP {}: {}".format(status, body)}
    operation_id = body["operation_id"]

    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        try:
            info = _get_json("{}/api/v1/workspaces/operations/create/{}".format(_api_base(), operation_id))
        except (urllib.error.URLError, OSError):
            time.sleep(5)
            continue
        if info.get("is_done"):
            return {"id": cid, "ok": True, "agent_id": info.get("agent_id"), "status": info.get("status")}
        if info.get("error"):
            return {"id": cid, "ok": False, "error": info.get("error"), "status": info.get("status")}
        time.sleep(5)
    return {"id": cid, "ok": False, "error": "timed out after {}s".format(int(poll_timeout))}


def self_check() -> None:
    assert load_personas_from_obj([{"id": "A B", "first_prompt": "hi"}])[0]["id"] == "a-b"
    assert load_personas_from_obj({"personas": [{"id": "x", "first_prompt": "y"}]})[0]["first_prompt"] == "y"
    try:
        load_personas_from_obj([{"id": "c", "first_prompt": "  "}])
        raise AssertionError("expected ValueError on empty first_prompt")
    except ValueError:
        pass
    assert [t["id"] for t in expand([{"id": "a", "persona": "", "first_prompt": "x"}], 1)] == ["a"]
    assert [t["id"] for t in expand([{"id": "a", "persona": "", "first_prompt": "x"}], 3)] == ["a-1", "a-2", "a-3"]
    pay = build_payload({"id": "a", "persona": "", "first_prompt": "x"}, "minds-eval-autosend", "sk-ant-K")
    assert pay["launch_mode"] == "MODAL" and pay["ai_provider"] == "API_KEY", pay
    assert pay["git_url"] == "/work/clones/a" and pay["host_name"] == "a", pay
    assert pay["branch"] == "minds-eval-autosend" and pay["anthropic_api_key"] == "sk-ant-K", pay
    print("self-check OK")


def main() -> None:
    ap = argparse.ArgumentParser(prog="mngr-minds-eval", description="Create one Modal workspace per persona x trial.")
    ap.add_argument("personas", nargs="?", help="path to personas.json ([{id,persona,first_prompt}, ...])")
    ap.add_argument("--repo", default="https://github.com/imbue-ai/forever-claude-template.git")
    ap.add_argument("--branch", default="minds-eval-autosend")
    ap.add_argument("-n", "--trials", type=int, default=1, help="workspaces to create per persona")
    ap.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))
    ap.add_argument("--self-check", action="store_true", help="run offline asserts and exit")
    args = ap.parse_args()

    if args.self_check:
        self_check()
        return
    if not args.personas:
        ap.error("personas.json path is required")
    if args.trials < 1:
        ap.error("--trials must be >= 1")
    if not args.api_key:
        ap.error("set --api-key or ANTHROPIC_API_KEY")

    personas = load_personas_from_obj(json.loads(Path(args.personas).read_text()))
    tasks = expand(personas, args.trials)
    CLONES_DIR.mkdir(parents=True, exist_ok=True)
    ensure_base(args.repo, args.branch)
    print(">> creating {} workspace(s): {} persona x {} trial ...".format(len(tasks), len(personas), args.trials))

    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
        futures = [pool.submit(create_one, t, args.branch, args.api_key) for t in tasks]
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            print("  [{}] {}: {}".format("OK " if r["ok"] else "ERR", r["id"], r.get("agent_id") or r.get("error")))

    ok = sum(1 for r in results if r["ok"])
    print(">> done: {}/{} workspaces created -- all in the one dashboard.".format(ok, len(results)))
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
