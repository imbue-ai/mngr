"""mngr_minds_eval -- Minds eval harness (CLI), run inside the minds-box container.

Subcommands:
  prepare-test-clones  Clone the FCT branch once per (persona x trial) and slot each persona's
                       config into scripts/first_command.json, committed. That's all -- creating
                       a Modal workspace off each prepared clone comes later.
  self-check           Run the offline asserts (persona loader, trial expansion, slug) and exit.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
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


def self_check() -> None:
    assert slugify("A B!") == "a-b"
    assert load_personas_from_obj([{"id": "A B", "first_prompt": "hi"}])[0]["id"] == "a-b"
    assert load_personas_from_obj({"personas": [{"id": "x", "first_prompt": "y"}]})[0]["first_prompt"] == "y"
    try:
        load_personas_from_obj([{"id": "c", "first_prompt": "  "}])
        raise AssertionError("expected ValueError on empty first_prompt")
    except ValueError:
        pass
    one = expand([{"id": "a", "persona": "p", "first_prompt": "x"}], 1)
    assert [t["id"] for t in one] == ["a"] and one[0]["first_prompt"] == "x", one
    three = expand([{"id": "a", "persona": "p", "first_prompt": "x"}], 3)
    assert [t["id"] for t in three] == ["a-1", "a-2", "a-3"], three
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


if __name__ == "__main__":
    main()
