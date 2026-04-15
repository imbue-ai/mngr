from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.errors import UserInputError

_MNGR_REPO_URL = "https://github.com/imbue-ai/mngr.git"


def ensure_mngr_clone(clone_dir: Path, cg: ConcurrencyGroup) -> None:
    """Ensure a shallow clone of the mngr repo exists at clone_dir.

    If the directory does not exist, performs a shallow clone.
    If it exists, verifies it is on the main branch and pulls.
    """
    if not clone_dir.exists():
        logger.info("Cloning mngr repository to {}...", clone_dir)
        cg.run_process_to_completion(
            ["git", "clone", "--depth", "1", _MNGR_REPO_URL, str(clone_dir)],
            timeout=120,
        )
        return

    # Verify the clone is on main
    result = cg.run_process_to_completion(
        ["git", "-C", str(clone_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        timeout=10,
    )
    branch = result.stdout.strip()
    if branch != "main":
        raise UserInputError(
            f"Clone at {clone_dir} is on branch '{branch}', expected 'main'. "
            "Delete the directory and try again."
        )

    logger.info("Updating existing clone at {}...", clone_dir)
    cg.run_process_to_completion(
        ["git", "-C", str(clone_dir), "pull", "--ff-only"],
        timeout=60,
    )
