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

from imbue.mngr_minds_eval import box as box_mod
from imbue.mngr_minds_eval import launch as launch_mod
from imbue.mngr_minds_eval import restore as restore_mod
from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import status as status_mod

DEFAULT_PORT_ENV = "MINDS_BARE_PORT"
# Set inside the box (the Dockerfile boots Minds with it); its absence means we are on the host.
IN_BOX = bool(os.environ.get(DEFAULT_PORT_ENV))


def _port() -> str:
    return os.environ.get(DEFAULT_PORT_ENV, "8420")


def _exec_in_box(container: str, mngr_branch: str, argv: list[str], personas: Path | None,
                 modal_user_id: str = "") -> None:
    """Host side: ensure the box exists, then run this same command inside it.

    `launch` and `restore` need the box's Minds API, its clone dir and its mngr -- so they run
    there. Status subcommands only read S3 and stay on the host. modal_user_id names the Modal
    environment the box's workspaces land in (eval flows pass the eval name).
    """
    import subprocess

    box_mod.ensure(container, mngr_branch, modal_user_id=modal_user_id)
    if personas is not None:
        subprocess.run(["docker", "cp", str(personas), "{}:/work/personas.json".format(container)], check=True)
        argv = ["/work/personas.json" if a == str(personas) else a for a in argv]
    command = ["docker", "exec", "-i"]
    if sys.stdout.isatty():
        command.append("-t")
    command += [
        "-e", "ANTHROPIC_API_KEY={}".format(os.environ.get("ANTHROPIC_API_KEY", "")),
        "-w", "/work/mngr", container,
        "uv", "run", "--package", "mngr-minds-eval", "minds-evals", *argv,
    ]
    returncode = subprocess.run(command).returncode
    if returncode == 0:
        # The workspaces run on Modal, but you view them through THIS box (Docker, on this machine):
        # its dashboard + mngr-forward proxy, both on localhost.
        box_mod.print_view_urls(container)
    sys.exit(returncode)


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _box_for(key: str, box_override: str = "") -> str:
    """Container name for a box keyed on `key` (an eval name for eval flows, a branch for utils)."""
    return box_override or "minds-box-{}".format(key.replace("/", "-"))


def _check_aws() -> dict:
    try:
        return s3_store.load_aws_env()
    except s3_store.AwsNotConfiguredError as exc:
        sys.exit("error: {}".format(exc))


def self_check() -> None:
    from imbue.mngr_minds_eval.launch import build_create_payload, load_cases

    env = {"AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK", "AWS_DEFAULT_REGION": "us-east-1",
           "MINDS_EVAL_BUCKET": "b"}
    assert s3_store.batch_prefix("web1", "20260713-101500") == "web1_20260713-101500"
    assert s3_store.split_batch("web1_20260713-101500") == ("web1", "20260713-101500")
    assert s3_store.case_prefix("web1_S", "web1", "todo") == "web1_S/web1_todo"
    assert s3_store.restic_repo_url(env, "web1_S/web1_todo") == \
        "s3:s3.us-east-1.amazonaws.com/b/web1_S/web1_todo/restic"

    payload = build_create_payload(Path("/work/clones/todo"), "EVAL-web1-CASE-todo", "sk-ant", "modal")
    assert payload["launch_mode"] == "MODAL" and payload["ai_provider"] == "API_KEY"
    assert payload["backup_provider"] == "CONFIGURE_LATER" and "backup_api_key_env" not in payload
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
    p_launch.add_argument("--mngr-branch", default="main", help="mngr branch the box runs (and vendors into cases)")
    p_launch.add_argument("--fct-branch", default=launch_mod.DEFAULT_FCT_BRANCH,
                          help="workspace-template branch each case is cloned from (must carry the eval worker)")
    p_launch.add_argument("--fct-repo", default=launch_mod.DEFAULT_FCT_REPO, help="workspace-template git URL")
    p_launch.add_argument("--box", default="", help="container name (default: minds-box-<mngr-branch>)")
    p_launch.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))

    p_box = sub.add_parser("box", help="build/boot a Minds box for an mngr branch (general utility)")
    p_box.add_argument("--mngr-branch", required=True, help="mngr branch the box runs")
    p_box.add_argument("--box", default="", help="container name (default: minds-box-<mngr-branch>)")

    p_clean = sub.add_parser("clean-modal-workspaces",
                             help="destroy ALL workspaces in an eval's Modal env (clean slate)")
    p_clean.add_argument("--name", required=True, help="eval name (its box + Modal env)")
    p_clean.add_argument("--mngr-branch", default="main", help="mngr branch to build the box if it isn't up")
    p_clean.add_argument("--box", default="", help="container name (default: minds-box-<name>)")

    p_ws = sub.add_parser("workspace", help="create ONE workspace in a box (general utility, no eval)")
    p_ws.add_argument("--mngr-branch", required=True, help="mngr branch the box runs")
    p_ws.add_argument("--box", default="", help="container name (default: minds-box-<mngr-branch>)")
    p_ws.add_argument("--fct-link", required=True, help="git URL or local path, passed to create verbatim")
    p_ws.add_argument("--fct-branch", default="", help="branch (blank for a local clone already on its commit)")
    p_ws.add_argument("--name", default="", help="workspace host name (blank = auto)")
    p_ws.add_argument("--compute", default="modal")
    p_ws.add_argument("--ai-provider", default="api_key", choices=("api_key", "subscription", "imbue_cloud"))
    p_ws.add_argument("--backup-provider", default="configure_later")
    p_ws.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))

    sub.add_parser("list-batches", help="list eval batches in S3")

    p_inspect = sub.add_parser("inspect", help="per-case status of a batch (from S3)")
    p_inspect.add_argument("batch", help="<eval>_<datetime>")

    p_restore = sub.add_parser("restore", help="restore a case snapshot into a fresh Modal workspace")
    p_restore.add_argument("batch")
    p_restore.add_argument("--case", required=True)
    p_restore.add_argument("--message", type=int, required=True, help="message index (post_message_<N>)")
    p_restore.add_argument("--mngr-branch", default="",
                           help="override; by default the mngr branch the batch was launched with")
    p_restore.add_argument("--box", default="", help="container name (default: minds-box-<mngr-branch>)")
    p_restore.add_argument("--restic-password", default=os.environ.get("RESTIC_PASSWORD", ""),
                           help="override; by default read from the batch config in S3")

    sub.add_parser("self-check", help="offline asserts")

    args = parser.parse_args()

    if args.command == "self-check":
        self_check()
        return
    # Box lifecycle (host-side): build/boot the box, then print how to view it. General utility --
    # spin up a Minds-on-a-branch to poke at, independent of any eval.
    if args.command == "box":
        if IN_BOX:
            parser.error("run `box` from the host, not inside a box")
        box = _box_for(args.mngr_branch, args.box)
        box_mod.ensure(box, args.mngr_branch)
        box_mod.print_view_urls(box)
        return
    if args.command == "clean-modal-workspaces":
        # Key on the eval name so it targets that run's box + Modal env (minds-<env>-<name>).
        if not IN_BOX:
            _exec_in_box(_box_for(args.name, args.box), args.mngr_branch, sys.argv[1:], None,
                         modal_user_id=args.name)
        launch_mod.destroy_all_workspaces(_port())
        return
    if args.command == "workspace":
        if not IN_BOX:
            _exec_in_box(_box_for(args.mngr_branch, args.box), args.mngr_branch, sys.argv[1:], None)
        from imbue.mngr_minds_eval import workspace as workspace_mod

        workspace_mod.create_workspace(
            port=_port(), fct_link=args.fct_link, fct_branch=args.fct_branch, name=args.name,
            compute=args.compute, ai_provider=args.ai_provider, anthropic_key=args.anthropic_key,
            backup_provider=args.backup_provider,
        )
        return

    # Status-only subcommands read S3 and need nothing else -- they run wherever they are invoked.
    if args.command == "list-batches":
        _check_aws()
        status_mod.list_batches()
        return
    if args.command == "inspect":
        _check_aws()
        status_mod.inspect(args.batch)
        return

    # launch / restore need the box (Minds API + clone dir + mngr). On the host: ensure the box,
    # then re-invoke this same command inside it.
    if args.command == "launch":
        _check_aws()
        if not args.anthropic_key:
            parser.error("set ANTHROPIC_API_KEY (or --anthropic-key)")
        if args.turns < 2:
            parser.error("--turns must be >= 2 (turn 1 sends the first prompt, the last ends the run)")
        if not IN_BOX:
            if not args.personas.is_file():
                parser.error("no such personas file: {}".format(args.personas))
            # The box + Modal env are keyed on the eval name: this run's sandboxes land in
            # minds-<env>-<name>, findable and separable from other evals.
            _exec_in_box(_box_for(args.name, args.box), args.mngr_branch, sys.argv[1:], args.personas,
                         modal_user_id=args.name)
        launch_mod.launch_batch(
            eval_name=args.name, personas_path=args.personas, anthropic_key=args.anthropic_key,
            num_turns=args.turns, compute="modal", port=_port(), stamp=_utc_stamp(),
            mngr_branch=args.mngr_branch, fct_repo=args.fct_repo, fct_branch=args.fct_branch,
        )
        return
    if args.command == "restore":
        env = _check_aws()
        if not IN_BOX:
            # Rebuild the SAME box the batch ran on: read its mngr branch + eval name from the batch
            # config in S3, so restore uses the right mngr and the run's own Modal env.
            config = s3_store.get_json(
                s3_store.make_client(env), env["MINDS_EVAL_BUCKET"],
                "{}/{}".format(args.batch, s3_store.BATCH_CONFIG_NAME),
            )
            if config is None:
                sys.exit("no such batch: {} (try: minds-evals list-batches)".format(args.batch))
            args.mngr_branch = args.mngr_branch or config.get("mngr_branch") or ""
            if not args.mngr_branch:
                sys.exit("batch {} does not record its mngr branch; pass --mngr-branch".format(args.batch))
            eval_name = config.get("eval_name") or s3_store.split_batch(args.batch)[0]
            print(">> batch ran on mngr {!r} (eval {!r})".format(args.mngr_branch, eval_name), flush=True)
            _exec_in_box(_box_for(eval_name, args.box), args.mngr_branch, sys.argv[1:], None,
                         modal_user_id=eval_name)
        restore_mod.restore(args.batch, args.case, args.message, port=_port(), restic_password=args.restic_password)


if __name__ == "__main__":
    main()
