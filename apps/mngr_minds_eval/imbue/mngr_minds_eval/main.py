"""minds-evals -- launch, inspect, evaluate, and visit Minds eval batches.

Host-native CLI. Each batch gets its own Modal env; boxes are full Minds computers in Docker
(headless for creating, a browser-accessible desktop for visiting).

  minds-evals launch --config eval-config.json    # create a batch (one workspace per case)
  minds-evals list-batches                        # S3 only
  minds-evals inspect combined_20260715-...       # per-case state, S3 only
  minds-evals evaluate combined_20260715-...      # score finished cases (ANTHROPIC_API_KEY)
  minds-evals visit-batch combined_20260715-...   # rebuild the batch's exact computer, enter it
  minds-evals box --mngr-branch main              # dev utility: a desktop box on a branch tip

Launched runs self-complete: the in-sandbox eval worker drives the conversation, snapshots /mngr
per turn (restic -> S3), and uploads the transcript -- so results are retrieved from S3 and the
launching machine does not need to stay on. `visit-batch` reads the batch's recorded mngr SHA and
Modal env from S3, boots a desktop box that IS that computer, and prints a noVNC URL: you enter a
real desktop running the Minds app and open the batch's workspaces as windows.
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
from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import status as status_mod

DEFAULT_PORT_ENV = "MINDS_BARE_PORT"
# Set inside a headless box (docker run -e); its absence means we are on the host.
IN_BOX = bool(os.environ.get(DEFAULT_PORT_ENV))
_CONFIG_IN_BOX = "/work/eval-config.json"


def _port() -> str:
    return os.environ.get(DEFAULT_PORT_ENV, "8420")


def _utc_stamp() -> str:
    # Microseconds so two launches in the same wall-clock second don't collide on one batch prefix.
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _check_aws() -> dict:
    try:
        return s3_store.load_aws_env()
    except s3_store.AwsNotConfiguredError as exc:
        sys.exit("error: {}".format(exc))


def _point_arg_to_box(argv: list[str], local: Path, box_path: str) -> list[str]:
    """Rewrite the uploaded file's CLI value to its in-box path, for any form the user typed
    (`X`, `./X`, `X/`, `--flag X`, `--flag=X`). Matches by Path, so `./eval-config.json` and
    `eval-config.json` (which `str(Path(...))` would render differently) both resolve to `local`."""
    out = []
    for token in argv:
        flag, sep, value = token.partition("=")
        if token == str(local) or Path(token) == local:
            out.append(box_path)
        elif sep and value and Path(value) == local:
            out.append("{}={}".format(flag, box_path))
        else:
            out.append(token)
    return out


def _run_in_container(container: str, argv: list[str], *, upload: tuple[Path, str] | None = None) -> int:
    """Re-run this same command inside an already-running headless box; return its exit code.
    upload = (local_path, box_path) copies a file in and rewrites its arg (the eval config)."""
    if upload is not None:
        local, box_path = upload
        subprocess.run(["docker", "cp", str(local), "{}:{}".format(container, box_path)], check=True)
        argv = _point_arg_to_box(argv, local, box_path)
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
    return subprocess.run(command).returncode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minds-evals", description="Launch, inspect, evaluate, and visit eval batches."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser("launch", help="launch an eval batch from a single config json")
    p_launch.add_argument(
        "--config",
        required=True,
        type=Path,
        help="eval config json: {name, mngr_branch, fct_branch?, fct_repo?, timeout_seconds?, personas:[...]}",
    )
    p_launch.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))
    # --stamp is internal: the host sets it when re-invoking launch inside the box.
    p_launch.add_argument("--stamp", default="", help=argparse.SUPPRESS)

    sub.add_parser("list-batches", help="list eval batches in S3")

    p_inspect = sub.add_parser("inspect", help="per-case status of a batch (from S3)")
    p_inspect.add_argument("batch", help="<eval>_<datetime>")

    p_eval = sub.add_parser("evaluate", help="score a finished batch (from S3; needs ANTHROPIC_API_KEY)")
    p_eval.add_argument("batch", help="<eval>_<datetime>")

    p_visit = sub.add_parser("visit-batch", help="rebuild a batch's exact Minds computer and enter its desktop")
    p_visit.add_argument("batch", help="<eval>_<datetime> (see list-batches)")

    p_box = sub.add_parser("box", help="dev utility: boot a desktop box on an mngr branch tip")
    p_box.add_argument("--mngr-branch", required=True)
    p_box.add_argument("--user-id", default="dev", help="Modal env suffix for this box (default: dev)")
    return parser


def _print_desktop_urls(container: str) -> None:
    url = box_mod.novnc_url(container)
    print("\n  enter the computer:  {}".format(url), flush=True)
    print("  (a real desktop running the Minds app; if the screen is blank, give it ~30s)", flush=True)
    print("  when done:  docker rm -f {}".format(container), flush=True)


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

    if args.command == "visit-batch":
        env = _check_aws()
        client = s3_store.make_client(env)
        config = s3_store.get_json(
            client, env["MINDS_EVAL_BUCKET"], "{}/{}".format(args.batch, s3_store.BATCH_CONFIG_NAME)
        )
        if config is None:
            sys.exit("no such batch: {} (try: minds-evals list-batches)".format(args.batch))
        branch = config.get("mngr_branch") or "main"
        ref = config.get("mngr_sha") or ""
        user_id = config.get("modal_user_id") or ""
        if not user_id:
            sys.exit("batch {} predates per-batch Modal envs -- relaunch it to make it visitable".format(args.batch))
        if not ref:
            print(">> batch has no recorded mngr sha; using the current tip of {}".format(branch), flush=True)
        container = box_mod.ensure(branch, user_id=user_id, ref=ref, desktop=True)
        _print_desktop_urls(container)
        return

    if args.command == "box":
        if IN_BOX:
            parser.error("run `box` from the host, not inside a box")
        container = box_mod.ensure(args.mngr_branch, user_id=box_mod.sanitize_user_id(args.user_id), desktop=True)
        _print_desktop_urls(container)
        return

    if args.command == "launch":
        _check_aws()
        # load_config validates on the host before any box is touched.
        config = launch_mod.load_config(args.config)
        if not args.anthropic_key:
            parser.error("set ANTHROPIC_API_KEY (or --anthropic-key)")
        if not IN_BOX:
            stamp = _utc_stamp()
            batch = s3_store.batch_prefix(config["name"], stamp)
            user_id = box_mod.sanitize_user_id(batch)
            container = box_mod.ensure(config["mngr_branch"], user_id=user_id, desktop=False)
            returncode = _run_in_container(
                container, sys.argv[1:] + ["--stamp", stamp], upload=(args.config, _CONFIG_IN_BOX)
            )
            if returncode == 0:
                # The launch box's job is done -- the workspaces self-complete on Modal and write to
                # S3. Visiting spins a fresh desktop box, so nothing needs to stay running.
                box_mod._run(["docker", "rm", "-f", container])
                print("\n  inspect:  minds-evals inspect {}".format(batch), flush=True)
                print("  visit:    minds-evals visit-batch {}".format(batch), flush=True)
            else:
                print("\n  launch failed -- the box was kept for debugging: docker logs {}".format(container))
            sys.exit(returncode)
        if not args.stamp:
            parser.error("--stamp is required inside the box (the host sets it)")
        launch_mod.launch_batch(config=config, anthropic_key=args.anthropic_key, port=_port(), stamp=args.stamp)
        return


if __name__ == "__main__":
    main()
