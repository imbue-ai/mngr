"""minds-evals -- launch and inspect Minds eval batches.

Host-native CLI. Box-using commands (launch/box/make-modal-workspace/clean-modal-workspaces) ensure
a Docker box (minds-box-<branch>-<sha>) and re-invoke themselves inside it; the S3-only commands
(list-batches/inspect/evaluate) never touch the box.

  minds-evals launch --config eval_config.json
  minds-evals list-batches
  minds-evals inspect web1_20260713-101500
  minds-evals evaluate web1_20260713-101500      # ANTHROPIC_API_KEY required
  minds-evals clean-modal-workspaces
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
from imbue.mngr_minds_eval import evaluate as evaluate_mod
from imbue.mngr_minds_eval import launch as launch_mod
from imbue.mngr_minds_eval import minds_client
from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import status as status_mod
from imbue.mngr_minds_eval import view as view_mod
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
        "-e",
        "ANTHROPIC_API_KEY={}".format(os.environ.get("ANTHROPIC_API_KEY", "")),
        "-w",
        "/work/mngr",
        container,
        "uv",
        "run",
        "--package",
        "mngr-minds-eval",
        "minds-evals",
        *argv,
    ]
    returncode = subprocess.run(command).returncode
    if returncode == 0:
        box_mod.print_view_urls(container)
    sys.exit(returncode)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minds-evals", description="Launch and inspect Minds eval batches.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser("launch", help="launch an eval batch from a single config json")
    p_launch.add_argument(
        "--config",
        required=True,
        type=Path,
        help="eval config json: {name, mngr_branch, fct_branch?, fct_repo?, personas:[{id, persona, prompts:[...]}]}",
    )
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

    sub.add_parser("clean-modal-workspaces", help="destroy ALL eval workspaces (the shared Modal env)")

    sub.add_parser("list-batches", help="list eval batches in S3")

    p_inspect = sub.add_parser("inspect", help="per-case status of a batch (from S3)")
    p_inspect.add_argument("batch", help="<eval>_<datetime>")

    p_eval = sub.add_parser("evaluate", help="score a finished batch (from S3; needs ANTHROPIC_API_KEY)")
    p_eval.add_argument("batch", help="<eval>_<datetime>")

    sub.add_parser("list-modal-workspaces", help="list workspaces in the shared Modal env (via a running box)")

    p_view = sub.add_parser("view-modal-workspace", help="open a scoped, self-authenticating view of one workspace")
    p_view.add_argument("name", help="workspace host name (see list-modal-workspaces)")
    p_view.add_argument("--box", default="", help="use this specific box (default: least-loaded running box)")
    p_view.add_argument(
        "--new-box-on-mngr-branch", default="", help="spin up a fresh box on this mngr branch to view from"
    )
    p_view.add_argument("--service", default="system_interface", help="which workspace service to forward")
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
    if args.command == "evaluate":
        _check_aws()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            parser.error("set ANTHROPIC_API_KEY -- the LLM-graded evals call the Anthropic API")
        evaluate_mod.evaluate_batch(args.batch)
        return
    if args.command == "list-modal-workspaces":
        view_mod.list_modal_workspaces()
        return
    if args.command == "view-modal-workspace":
        view_mod.view_modal_workspace(
            args.name, box=args.box, new_box_on_mngr_branch=args.new_box_on_mngr_branch, service=args.service
        )
        return

    if args.command == "box":
        if IN_BOX:
            parser.error("run `box` from the host, not inside a box")
        box_mod.print_view_urls(box_mod.ensure(args.mngr_branch))
        return

    if args.command == "clean-modal-workspaces":
        if not IN_BOX:
            box = box_mod.find_any_running()
            if not box:
                sys.exit("no running box -- start one (minds-evals box --mngr-branch <X>) or launch a batch first")
            _run_in_container(box, sys.argv[1:])
        launch_mod.destroy_all_workspaces(_port())
        return

    if args.command == "make-modal-workspace":
        if not IN_BOX:
            _run_in_container(box_mod.ensure(args.mngr_branch), sys.argv[1:])
        try:
            workspace_mod.create_workspace(
                port=_port(),
                fct_link=args.fct_link,
                fct_branch=args.fct_branch,
                name=args.name,
                ai_provider=args.ai_provider,
                anthropic_key=args.anthropic_key,
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
            _run_in_container(
                box_mod.ensure(config["mngr_branch"]), sys.argv[1:], upload=(args.config, _CONFIG_IN_BOX)
            )
        launch_mod.launch_batch(config=config, anthropic_key=args.anthropic_key, port=_port(), stamp=_utc_stamp())
        return


if __name__ == "__main__":
    main()
