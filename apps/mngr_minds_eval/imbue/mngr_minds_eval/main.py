"""minds-evals -- launch and inspect Minds eval batches.

Runs inside the minds-box container (the create API and clone paths are container-local).

  minds-evals launch --name web1 --personas sample-personas.json --turns 4
  minds-evals list-batches
  minds-evals inspect web1_20260713-101500
  minds-evals restore web1_20260713-101500 --case todo-app --message 2

Launched runs self-complete: the in-sandbox eval worker drives the conversation, snapshots /mngr
per turn (restic -> S3), and uploads the transcript -- so results are retrieved from S3 and the
launching machine does not need to stay on.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

from imbue.mngr_minds_eval import launch as launch_mod
from imbue.mngr_minds_eval import restore as restore_mod
from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import status as status_mod

DEFAULT_PORT_ENV = "MINDS_BARE_PORT"


def _port() -> str:
    return os.environ.get(DEFAULT_PORT_ENV, "8420")


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _check_aws() -> dict:
    try:
        return s3_store.load_aws_env()
    except s3_store.AwsNotConfiguredError as exc:
        sys.exit("error: {}".format(exc))


def self_check() -> None:
    from imbue.mngr_minds_eval.launch import backup_env_block, build_create_payload, load_cases

    env = {"AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK", "AWS_DEFAULT_REGION": "us-east-1",
           "MINDS_EVAL_BUCKET": "b"}
    assert s3_store.batch_prefix("web1", "20260713-101500") == "web1_20260713-101500"
    assert s3_store.split_batch("web1_20260713-101500") == ("web1", "20260713-101500")
    assert s3_store.case_prefix("web1_S", "web1", "todo") == "web1_S/web1_todo"
    assert s3_store.restic_repo_url(env, "web1_S/web1_todo") == \
        "s3:s3.us-east-1.amazonaws.com/b/web1_S/web1_todo/restic"

    block = backup_env_block(env, "s3:repo", "pw")
    assert "RESTIC_REPOSITORY=s3:repo" in block and "RESTIC_PASSWORD=pw" in block
    assert "AWS_ACCESS_KEY_ID=AK" in block and "AWS_SECRET_ACCESS_KEY=SK" in block

    payload = build_create_payload(Path("/work/clones/todo"), "EVAL-web1-CASE-todo", "sk-ant", "modal", block)
    assert payload["launch_mode"] == "MODAL" and payload["ai_provider"] == "API_KEY"
    assert payload["backup_provider"] == "API_KEY" and payload["backup_api_key_env"] == block
    assert payload["branch"] == "" and payload["git_url"] == "/work/clones/todo"

    import json as _json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.json"
        path.write_text(_json.dumps([{"id": "a", "persona": "p", "first_prompt": "go"}]))
        cases = load_cases(path)
        assert cases == [{"id": "a", "persona": "p", "first_prompt": "go"}], cases
        path.write_text(_json.dumps([{"id": "a", "first_prompt": " "}]))
        try:
            load_cases(path)
            raise AssertionError("expected ValueError on empty first_prompt")
        except ValueError:
            pass
    print("self-check OK")


def main() -> None:
    parser = argparse.ArgumentParser(prog="minds-evals", description="Launch and inspect Minds eval batches.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser("launch", help="launch an eval batch (one self-completing workspace per case)")
    p_launch.add_argument("--name", required=True, help="eval name; batch folder is <name>_<utc-datetime>")
    p_launch.add_argument("--personas", required=True, type=Path, help="config json: [{id, persona, first_prompt}]")
    p_launch.add_argument("--turns", type=int, default=4, help="waits the responder sits through (default 4)")
    p_launch.add_argument("--compute", default="modal", choices=("modal", "docker"))
    p_launch.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))

    sub.add_parser("list-batches", help="list eval batches in S3")

    p_inspect = sub.add_parser("inspect", help="per-case status of a batch (from S3)")
    p_inspect.add_argument("batch", help="<eval>_<datetime>")

    p_restore = sub.add_parser("restore", help="restore a case snapshot into a local docker workspace")
    p_restore.add_argument("batch")
    p_restore.add_argument("--case", required=True)
    p_restore.add_argument("--message", type=int, required=True, help="message index (post_message_<N>)")
    p_restore.add_argument("--restic-password", default=os.environ.get("RESTIC_PASSWORD", ""),
                           help="override; by default read from the batch config in S3")

    sub.add_parser("self-check", help="offline asserts")

    args = parser.parse_args()

    if args.command == "self-check":
        self_check()
        return
    if args.command == "launch":
        _check_aws()
        if not args.anthropic_key:
            parser.error("set ANTHROPIC_API_KEY (or --anthropic-key)")
        if args.turns < 2:
            parser.error("--turns must be >= 2 (turn 1 sends the first prompt, the last ends the run)")
        if not args.personas.is_file():
            parser.error("no such personas file: {}".format(args.personas))
        launch_mod.launch_batch(
            eval_name=args.name, personas_path=args.personas, anthropic_key=args.anthropic_key,
            num_turns=args.turns, compute=args.compute, port=_port(), stamp=_utc_stamp(),
        )
        return
    if args.command == "list-batches":
        _check_aws()
        status_mod.list_batches()
        return
    if args.command == "inspect":
        _check_aws()
        status_mod.inspect(args.batch)
        return
    if args.command == "restore":
        _check_aws()
        restore_mod.restore(args.batch, args.case, args.message, port=_port(), restic_password=args.restic_password)


if __name__ == "__main__":
    main()
