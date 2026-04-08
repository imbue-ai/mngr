"""Utility functions for the test-mapreduce plugin."""

import secrets
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import TransferMode

_SHORT_ID_LENGTH = 6


def resolve_templates(
    template_names: tuple[str, ...],
    config: MngrConfig,
) -> dict[str, object]:
    """Resolve create templates by name and merge their options.

    Later templates override earlier ones for the same key.
    Returns a merged dict of template option values.
    """
    merged: dict[str, object] = {}
    for template_name in template_names:
        key = CreateTemplateName(template_name)
        if key not in config.create_templates:
            available = [str(t) for t in config.create_templates]
            avail_str = f" Available: {', '.join(available)}" if available else ""
            raise MngrError(f"Template '{template_name}' not found.{avail_str}")
        for k, v in config.create_templates[key].options.items():
            if v is not None:
                merged[k] = v
    return merged


def short_random_id() -> str:
    """Generate a short random hex suffix for agent name uniqueness."""
    return secrets.token_hex(_SHORT_ID_LENGTH // 2)


class CollectTestsError(MngrError, RuntimeError):
    """Raised when pytest test collection fails."""

    ...


def get_base_commit(source_dir: Path, cg: ConcurrencyGroup) -> str:
    """Get the current HEAD commit hash, used as the base for all agent branches."""
    result = cg.run_process_to_completion(["git", "rev-parse", "HEAD"], cwd=source_dir)
    return result.stdout.strip()


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


def sanitize_test_name_for_agent(test_node_id: str) -> str:
    """Convert a pytest node ID into a valid agent name suffix.

    Strips the file path prefix and replaces characters that are not valid in
    agent names.
    """
    parts = test_node_id.split("::")
    short_name = parts[-1] if parts else test_node_id
    cleaned = ""
    for ch in short_name:
        if ch.isalnum() or ch == "-":
            cleaned += ch
        else:
            cleaned += "-"
    sanitized = ""
    for ch in cleaned:
        if ch == "-" and sanitized.endswith("-"):
            continue
        sanitized += ch
    return sanitized.strip("-").lower()[:40]


def transfer_mode_for_provider(provider_name: ProviderInstanceName) -> TransferMode:
    """Determine the transfer mode based on the provider.

    GIT_WORKTREE only works when source and target are on the same host, so it is
    only usable with the local provider. Remote providers (docker, modal, etc.)
    use GIT_MIRROR to transfer git history efficiently.
    """
    is_local = provider_name.lower() == LOCAL_PROVIDER_NAME
    return TransferMode.GIT_WORKTREE if is_local else TransferMode.GIT_MIRROR
