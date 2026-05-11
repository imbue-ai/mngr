#!/usr/bin/env python3
"""Generate an ed25519 SSH keypair for pool host management.

Saves the keypair to a local directory and prints instructions for uploading
the private key to Modal as a secret and using the public key with the pool
creation script.

Usage:
    uv run python apps/remote_service_connector/scripts/generate_management_key.py
    uv run python apps/remote_service_connector/scripts/generate_management_key.py --output-dir ./my_keys
"""

import subprocess
import sys
from pathlib import Path
from typing import Final

import click
from loguru import logger

_DEFAULT_OUTPUT_DIR: Final[str] = "./management_key"
_KEY_FILENAME: Final[str] = "id_ed25519"
_SSH_KEYGEN_TIMEOUT_SECONDS: Final[int] = 30


@click.command()
@click.option(
    "--output-dir",
    type=click.Path(),
    default=_DEFAULT_OUTPUT_DIR,
    help="Directory to write the keypair into",
)
def generate_management_key(output_dir: str) -> None:
    output_path = Path(output_dir)
    private_key_path = output_path / _KEY_FILENAME
    public_key_path = output_path / f"{_KEY_FILENAME}.pub"

    if private_key_path.exists() or public_key_path.exists():
        logger.error(
            "Key files already exist in {}. Remove them first if you want to regenerate.",
            output_path,
        )
        sys.exit(1)

    output_path.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(private_key_path), "-N", "", "-C", "pool-management-key"],
        capture_output=True,
        text=True,
        timeout=_SSH_KEYGEN_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.error("ssh-keygen failed: {}", result.stderr)
        sys.exit(1)

    public_key_text = public_key_path.read_text().strip()

    logger.info("Keypair generated in {}/", output_path)
    logger.info("  Private key: {}", private_key_path)
    logger.info("  Public key:  {}", public_key_path)
    logger.info("Public key contents:")
    logger.info("  {}", public_key_text)
    logger.info("")
    logger.info("Next steps:")
    logger.info("")
    logger.info("  1. Upload the private key to Modal as a secret:")
    logger.info("")
    logger.info("     Create a .minds/<env>/pool-ssh.sh file with:")
    logger.info('       export POOL_SSH_PRIVATE_KEY="$(cat {})"', private_key_path)
    logger.info("")
    logger.info("     Then push it:")
    logger.info("       uv run scripts/push_modal_secrets.py <env>")
    logger.info("")
    logger.info("  2. Pass the public key file to the pool bake command:")
    logger.info("     uv run mngr imbue_cloud admin pool create \\")
    logger.info("       --count 3 \\")
    logger.info('       --attributes \'{"repo_branch_or_tag": "<branch-or-tag>"}\' \\')
    logger.info("       --workspace-dir <path/to/forever-claude-template> \\")
    logger.info("       --management-public-key-file {} \\", public_key_path)
    logger.info("       --database-url $DATABASE_URL")
    logger.info("")
    logger.info("  3. Keep the private key secure. Do not commit it to the repository.")


if __name__ == "__main__":
    generate_management_key()
