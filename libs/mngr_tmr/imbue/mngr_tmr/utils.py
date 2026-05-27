"""Pytest discovery helpers used by the TMR recipe."""

from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.errors import MngrError


class CollectTestsError(MngrError, RuntimeError):
    """Raised when pytest test collection fails."""

    ...


def collect_tests(
    pytest_args: tuple[str, ...],
    source_dir: Path,
    cg: ConcurrencyGroup,
) -> list[str]:
    """Run pytest --collect-only -q and return the list of test node IDs."""
    cmd = ["python", "-m", "pytest", "--collect-only", "-q", *pytest_args]
    logger.info("Collecting tests: {}", " ".join(cmd))
    result = cg.run_process_to_completion(cmd, cwd=source_dir, timeout=60.0, is_checked_after=False)
    if result.returncode != 0:
        raise CollectTestsError(f"pytest --collect-only failed (exit code {result.returncode}):\n{result.stderr}")

    test_ids: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped and "::" in stripped and not stripped.startswith("="):
            test_ids.append(stripped)

    if not test_ids:
        raise CollectTestsError("pytest --collect-only returned no tests")

    logger.info("Collected {} test(s)", len(test_ids))
    return test_ids
