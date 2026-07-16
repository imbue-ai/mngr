"""minds-evals -- launch, inspect, evaluate, and visit Minds eval batches. Everything on Modal.

Host-native CLI; your machine only makes API calls. Each batch gets its own Modal env, and every
box is a full Minds computer running as a Modal sandbox: the real Minds app on a virtual desktop,
streamed to your browser through Modal's encrypted tunnel (one https URL, works from anywhere).
`launch` creates the batch's workspaces inside that computer and leaves it running for you to
watch; `visit-batch` finds or reboots the same computer later; `stop` kills it early.

  minds-evals launch trio --config eval-config.json   (create a batch: one workspace per case)
  minds-evals list-batches / inspect trio / evaluate trio   (S3-only reads + scoring)
  minds-evals visit-batch trio                        (the batch's computer, in your browser)
  minds-evals stop trio                               (terminate the batch's box; workspaces live on)
  minds-evals box --mngr-branch main                  (dev utility: a desktop box on a branch tip)

Launched runs self-complete: the in-sandbox eval worker drives the conversation, snapshots /mngr
per turn (restic -> S3), and uploads the transcript -- so results are retrieved from S3 and no box
needs to stay alive for them.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from imbue.mngr_minds_eval import box as box_mod
from imbue.mngr_minds_eval import evaluate as evaluate_mod
from imbue.mngr_minds_eval import launch as launch_mod
from imbue.mngr_minds_eval import minds_client
from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import status as status_mod
from imbue.mngr_minds_eval import workspace

# Set inside every box sandbox (see box._box_env); its absence means we are on the host.
IN_BOX = bool(os.environ.get("MINDS_EVAL_IN_BOX"))
_CONFIG_IN_BOX = "/work/eval-config.json"


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minds-evals", description="Launch, inspect, evaluate, and visit eval batches (all on Modal)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser("launch", help="launch an eval batch: a unique name + a config template")
    p_launch.add_argument("name", help="unique batch name (lowercase/digits/dashes) -- the S3 prefix and Modal env")
    p_launch.add_argument(
        "--config",
        required=True,
        type=Path,
        help="eval config json: {mngr_branch, fct_branch?, fct_repo?, timeout_seconds?, personas:[...]}",
    )
    p_launch.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))

    sub.add_parser("list-batches", help="list eval batches in S3")

    p_inspect = sub.add_parser("inspect", help="per-case status of a batch (from S3)")
    p_inspect.add_argument("batch", help="the eval name")

    p_eval = sub.add_parser("evaluate", help="score a finished batch (from S3; needs ANTHROPIC_API_KEY)")
    p_eval.add_argument("batch", help="the eval name")

    p_visit = sub.add_parser("visit-batch", help="the batch's exact Minds computer, in your browser")
    p_visit.add_argument("batch", help="the eval name (see list-batches)")

    p_stop = sub.add_parser("stop", help="terminate a batch's box sandbox (its workspaces live on)")
    p_stop.add_argument("batch", help="the eval name (or a box user-id)")

    p_box = sub.add_parser("box", help="dev utility: boot a desktop box on an mngr branch tip")
    p_box.add_argument("--mngr-branch", required=True)
    p_box.add_argument("--user-id", default="minh", help="Modal env suffix for this box (default: minh)")
    p_box.add_argument("--dwt-link", default="", help="also create ONE workspace from this template repo/path")
    p_box.add_argument("--dwt-branch", default="", help="template branch for --dwt-link (blank = repo default)")
    p_box.add_argument("--workspace-name", default="", help="host name for the --dwt-link workspace (blank = auto)")
    return parser


def _print_desktop_urls(sandbox) -> None:
    print("\n  enter the computer:  {}".format(box_mod.novnc_url(sandbox)), flush=True)
    print("  (a real desktop running the Minds app; if the screen is blank, give it ~30s)", flush=True)
    print(
        "  box sandbox: {}  (auto-dies in {}h; stop early with: minds-evals stop <name>)".format(
            sandbox.object_id, box_mod.BOX_TIMEOUT_HOURS
        ),
        flush=True,
    )


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

    if args.command == "stop":
        sandbox = box_mod.find_box(args.batch)
        if sandbox is None:
            sys.exit("no running box for {!r}".format(args.batch))
        sandbox.terminate()
        print(
            "terminated box {} for {!r} (its workspaces live on; visit-batch reboots it)".format(
                sandbox.object_id, args.batch
            ),
            flush=True,
        )
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
        sandbox = box_mod.ensure(branch, user_id=user_id, ref=ref)
        _print_desktop_urls(sandbox)
        return

    if args.command == "box":
        if IN_BOX:
            # The in-box leg of --dwt-link: find the Minds app's API and create the one workspace.
            if not args.dwt_link:
                parser.error("run `box` from the host, not inside a box")
            try:
                workspace.create_workspace(
                    port=minds_client.discover_api_port(),
                    fct_link=args.dwt_link,
                    fct_branch=args.dwt_branch,
                    name=args.workspace_name,
                    anthropic_key=os.environ.get("ANTHROPIC_API_KEY", ""),
                )
            except minds_client.CreateError as exc:
                sys.exit(str(exc))
            return
        if args.dwt_link and not os.environ.get("ANTHROPIC_API_KEY"):
            parser.error("--dwt-link creates an api_key workspace -- set ANTHROPIC_API_KEY")
        sandbox = box_mod.ensure(args.mngr_branch, user_id=box_mod.sanitize_user_id(args.user_id))
        _print_desktop_urls(sandbox)
        if args.dwt_link:
            returncode = box_mod.run_in_box(
                sandbox, sys.argv[1:], {"ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")}
            )
            sys.exit(returncode)
        return

    if args.command == "launch":
        env = _check_aws()
        # Validate the name and the config template on the host before any sandbox is touched. The
        # name is the batch id: it IS the S3 prefix and the Modal env.
        batch = launch_mod.validate_name(args.name)
        # load_config validates the template shape up front.
        config = launch_mod.load_config(args.config)
        if not args.anthropic_key:
            parser.error("set ANTHROPIC_API_KEY (or --anthropic-key)")
        if not IN_BOX:
            # Eval names are unique, hard requirement: the S3 batch must not exist, and creating the
            # batch's Modal env is an atomic claim (fails out on a collision). Pre-creating the env
            # also lets every workspace create fan out concurrently.
            client = s3_store.make_client(env)
            if (
                s3_store.get_json(client, env["MINDS_EVAL_BUCKET"], "{}/{}".format(batch, s3_store.BATCH_CONFIG_NAME))
                is not None
            ):
                sys.exit(
                    "batch {!r} already exists in s3://{}/ -- eval names are unique; pick a new name "
                    "(or delete the old batch prefix and its Modal env {} first)".format(
                        batch, env["MINDS_EVAL_BUCKET"], box_mod.modal_env_name(batch)
                    )
                )
            try:
                print(">> claiming Modal env {} ...".format(box_mod.modal_env_name(batch)), flush=True)
                box_mod.create_modal_env(batch)
            except box_mod.BoxError as exc:
                sys.exit(str(exc))
            sandbox = box_mod.ensure(config["mngr_branch"], user_id=batch)
            # The box IS this batch's computer and it is up NOW -- print its desktop before the
            # creates run, so you can enter it and watch the workspaces appear as they are made.
            _print_desktop_urls(sandbox)
            box_mod.write_file(sandbox, _CONFIG_IN_BOX, args.config.read_text())
            argv = _point_arg_to_box(sys.argv[1:], args.config, _CONFIG_IN_BOX)
            returncode = box_mod.run_in_box(sandbox, argv, {"ANTHROPIC_API_KEY": args.anthropic_key})
            if returncode == 0:
                print("\n  inspect:  minds-evals inspect {}".format(batch), flush=True)
                print("  enter its computer any time:  minds-evals visit-batch {}".format(batch), flush=True)
            else:
                print("\n  launch failed -- see: modal sandbox logs {}".format(sandbox.object_id), flush=True)
            sys.exit(returncode)
        # Inside the box: the Minds app booted its backend on a port of its choosing -- find it.
        port = minds_client.discover_api_port()
        launch_mod.launch_batch(name=batch, config=config, anthropic_key=args.anthropic_key, port=port)
        return


if __name__ == "__main__":
    main()
