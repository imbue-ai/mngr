"""minds-evals -- launch and inspect Minds eval batches.

Host-native CLI. Box-using commands (launch/box/make-modal-workspace/clean-modal-workspaces/restore)
ensure a Docker box (minds-box-<branch>-<sha>) and re-invoke themselves inside it; status commands
(list-batches/inspect) only read S3.

  minds-evals launch --config eval_config.json
  minds-evals list-batches
  minds-evals inspect web1_20260713-101500
  minds-evals restore web1_20260713-101500 --case todo-app --message 2
  minds-evals clean-modal-workspaces --mngr-branch minds-eval
  minds-evals box --mngr-branch minds-eval
  minds-evals make-modal-workspace --mngr-branch minds-eval --fct-link <url> --fct-branch main

Launched runs self-complete: the in-sandbox eval worker drives the conversation, snapshots /mngr
per turn (restic -> S3), and uploads the transcript -- so results are retrieved from S3 and the
launching machine does not need to stay on.
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

from imbue.mngr_minds_eval import box as box_mod
from imbue.mngr_minds_eval import launch as launch_mod
from imbue.mngr_minds_eval import minds_client
from imbue.mngr_minds_eval import restore as restore_mod
from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import status as status_mod
from imbue.mngr_minds_eval import workspace as workspace_mod

DEFAULT_PORT_ENV = "MINDS_BARE_PORT"
# Set inside the box (the Dockerfile boots Minds with it); its absence means we are on the host.
IN_BOX = bool(os.environ.get(DEFAULT_PORT_ENV))
_CONFIG_IN_BOX = "/work/eval-config.json"


def _port() -> str:
    return os.environ.get(DEFAULT_PORT_ENV, "8420")


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _check_aws() -> dict:
    try:
        return s3_store.load_aws_env()
    except s3_store.AwsNotConfiguredError as exc:
        sys.exit("error: {}".format(exc))


def _run_in_container(container: str, argv: list[str], *, upload: tuple[Path, str] | None = None) -> None:
    """Run this same command inside an already-resolved box, then print how to view the workspaces.
    upload = (local_path, box_path) copies a file in and rewrites its arg (the eval config)."""
    if upload is not None:
        local, box_path = upload
        subprocess.run(["docker", "cp", str(local), "{}:{}".format(container, box_path)], check=True)
        argv = [box_path if a == str(local) else a for a in argv]
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
        box_mod.print_view_urls(container)
    sys.exit(returncode)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minds-evals", description="Launch and inspect Minds eval batches.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser("launch", help="launch an eval batch from a single config json")
    p_launch.add_argument("--config", required=True, type=Path,
                          help="eval config json: {name, turns, mngr_branch, fct_branch?, fct_repo?, personas:[...]}")
    p_launch.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))

    p_box = sub.add_parser("box", help="build/boot a Minds box for an mngr branch (general utility)")
    p_box.add_argument("--mngr-branch", required=True)

    p_ws = sub.add_parser("make-modal-workspace", help="create ONE Modal workspace in a box (utility, no eval)")
    p_ws.add_argument("--mngr-branch", required=True)
    p_ws.add_argument("--fct-link", required=True, help="git URL or local path, passed to create verbatim")
    p_ws.add_argument("--fct-branch", default="", help="branch (blank for a local clone already on its commit)")
    p_ws.add_argument("--name", default="", help="workspace host name (blank = auto)")
    p_ws.add_argument("--ai-provider", default="api_key", choices=("api_key", "subscription", "imbue_cloud"))
    p_ws.add_argument("--backup-provider", default="configure_later")
    p_ws.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))

    p_clean = sub.add_parser("clean-modal-workspaces",
                             help="destroy ALL workspaces in a branch's Modal env (clean slate)")
    p_clean.add_argument("--mngr-branch", required=True)

    sub.add_parser("list-batches", help="list eval batches in S3")

    p_inspect = sub.add_parser("inspect", help="per-case status of a batch (from S3)")
    p_inspect.add_argument("batch", help="<eval>_<datetime>")

    p_restore = sub.add_parser("restore", help="restore a case snapshot into a fresh Modal workspace")
    p_restore.add_argument("batch")
    p_restore.add_argument("--case", required=True)
    p_restore.add_argument("--message", type=int, required=True, help="message index (post_message_<N>)")
    p_restore.add_argument("--restic-password", default="", help="override; by default read from the batch config")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "list-batches":
        _check_aws()
        status_mod.list_batches()
        return
    if args.command == "inspect":
        _check_aws()
        status_mod.inspect(args.batch)
        return

    if args.command == "box":
        if IN_BOX:
            parser.error("run `box` from the host, not inside a box")
        box_mod.print_view_urls(box_mod.ensure(args.mngr_branch))
        return

    if args.command == "clean-modal-workspaces":
        # Clean only needs the branch's Modal env, which any box for the branch can reach -- so reuse
        # a running box at any SHA, and only build a fresh tip box if none is up.
        if not IN_BOX:
            _run_in_container(box_mod.find_running(args.mngr_branch) or box_mod.ensure(args.mngr_branch),
                              sys.argv[1:])
        launch_mod.destroy_all_workspaces(_port())
        return

    if args.command == "make-modal-workspace":
        if not IN_BOX:
            _run_in_container(box_mod.ensure(args.mngr_branch), sys.argv[1:])
        try:
            workspace_mod.create_workspace(
                port=_port(), fct_link=args.fct_link, fct_branch=args.fct_branch, name=args.name,
                ai_provider=args.ai_provider, anthropic_key=args.anthropic_key,
                backup_provider=args.backup_provider,
            )
        except minds_client.CreateError as exc:
            sys.exit(str(exc))
        return

    if args.command == "launch":
        _check_aws()
        config = launch_mod.load_config(args.config)  # validates on the host before touching the box
        if not args.anthropic_key:
            parser.error("set ANTHROPIC_API_KEY (or --anthropic-key)")
        if not IN_BOX:
            _run_in_container(box_mod.ensure(config["mngr_branch"]), sys.argv[1:],
                              upload=(args.config, _CONFIG_IN_BOX))
        launch_mod.launch_batch(config=config, anthropic_key=args.anthropic_key, port=_port(), stamp=_utc_stamp())
        return

    if args.command == "restore":
        env = _check_aws()
        if not IN_BOX:
            # Rebuild the SAME box the batch ran on, at its EXACT recorded SHA, so restore always
            # uses the exact mngr. Branch + SHA both come from the batch config in S3.
            config = s3_store.get_json(s3_store.make_client(env), env["MINDS_EVAL_BUCKET"],
                                       "{}/{}".format(args.batch, s3_store.BATCH_CONFIG_NAME))
            if config is None:
                sys.exit("no such batch: {} (try: minds-evals list-batches)".format(args.batch))
            mngr_branch = config.get("mngr_branch")
            if not mngr_branch:
                sys.exit("batch {} does not record its mngr branch".format(args.batch))
            print(">> restoring on mngr {!r} @ {}".format(mngr_branch, (config.get("mngr_sha") or "tip")[:12]), flush=True)
            _run_in_container(box_mod.ensure(mngr_branch, config.get("mngr_sha", "")), sys.argv[1:])
        restore_mod.restore(args.batch, args.case, args.message, port=_port(), restic_password=args.restic_password)


if __name__ == "__main__":
    main()
