"""Set up a dedicated mngr host_dir for inspecting TMR CI's modal agents.

CI runs TMR with MNGR_USER_ID=tmr-ci so all CI-created modal agents share a
single namespace. To see them locally, point MNGR_HOST_DIR at the host_dir
this script creates, then run normal mngr commands (mngr list, mngr connect,
etc.).

Run from the repo root:

    uv run --project libs/mngr_tmr python libs/mngr_tmr/scripts/setup_tmr_ci_debug.py

The script is idempotent. Re-run it any time to re-print the modal SSH
public key (e.g. after a teammate asks you to send it for the authorized
keys list).
"""

import argparse
import sys
from pathlib import Path

from imbue.mngr.config.consts import PROFILES_DIRNAME
from imbue.mngr.config.consts import ROOT_CONFIG_FILENAME
from imbue.mngr.config.data_types import USER_ID_FILENAME
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair

CI_USER_ID = "tmr-ci"
CI_PROFILE_ID = "tmr-ci"
DEFAULT_HOST_DIR = Path.home() / ".mngr-tmr-ci"


def setup(host_dir: Path) -> str:
    profile_dir = host_dir / PROFILES_DIRNAME / CI_PROFILE_ID
    modal_keys_dir = profile_dir / "providers" / "modal"
    profile_dir.mkdir(parents=True, exist_ok=True)

    config_path = host_dir / ROOT_CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(f'profile = "{CI_PROFILE_ID}"\n')

    user_id_path = profile_dir / USER_ID_FILENAME
    if not user_id_path.exists():
        user_id_path.write_text(CI_USER_ID)

    _, public_key = load_or_create_ssh_keypair(modal_keys_dir, key_name="modal_ssh_key")
    return public_key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--host-dir",
        type=Path,
        default=DEFAULT_HOST_DIR,
        help=f"Where to put the CI debug host_dir [default: {DEFAULT_HOST_DIR}]",
    )
    args = parser.parse_args()

    host_dir: Path = args.host_dir.expanduser().resolve()
    public_key = setup(host_dir)

    print(f"Host dir: {host_dir}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Add the following line to .github/tmr-authorized-keys (one PR per teammate):", file=sys.stderr)
    print("", file=sys.stderr)
    print(public_key)
    print("", file=sys.stderr)
    print("Once your key is merged, inspect CI agents with:", file=sys.stderr)
    print(f"  MNGR_HOST_DIR={host_dir} uv run mngr list", file=sys.stderr)
    print(f"  MNGR_HOST_DIR={host_dir} uv run mngr connect <agent-id>", file=sys.stderr)


if __name__ == "__main__":
    main()
