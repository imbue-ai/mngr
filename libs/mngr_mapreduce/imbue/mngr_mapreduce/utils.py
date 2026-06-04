"""Utility functions for the mngr-mapreduce framework."""

import itertools
from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.errors import MngrError


def make_run_name() -> str:
    """Compact timestamp identifying a map-reduce run, e.g. '20260514184215'.

    14 chars, all digits, sortable by alphabetical comparison. UTC.
    """
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")


def dedup_name(base: str, used: set[str]) -> str:
    """Return ``base`` if unused, else ``base-2``, ``base-3``, ... .

    Mutates ``used`` to include the returned value. Used to keep agent /
    branch names unique within a single run after sanitization truncation
    may have collapsed two distinct task ids onto the same suffix.
    """
    if base not in used:
        used.add(base)
        return base
    for counter in itertools.count(2):
        candidate = f"{base}-{counter}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise AssertionError("itertools.count is infinite; loop must return")


def sanitize_for_agent_name(raw: str) -> str:
    """Convert an arbitrary task identifier into a valid agent-name suffix.

    Replaces non-alphanumeric (and non-hyphen) characters with hyphens,
    collapses runs of hyphens, strips leading/trailing hyphens, lowercases,
    and truncates to 40 chars. The trailing-hyphen strip is also re-applied
    after truncation, since the truncation can leave a dangling hyphen
    (which AgentName rejects -- "alphanumeric with dashes/underscores in
    the middle"). For example, "test-create-modal-idle-mode-ssh-timeout-300"
    must come back as "test-create-modal-idle-mode-ssh-timeout", not
    "test-create-modal-idle-mode-ssh-timeout-".
    """
    cleaned = ""
    for ch in raw:
        if ch.isalnum() or ch == "-":
            cleaned += ch
        else:
            cleaned += "-"
    sanitized = ""
    for ch in cleaned:
        if ch == "-" and sanitized.endswith("-"):
            continue
        sanitized += ch
    return sanitized.strip("-").lower()[:40].rstrip("-")


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


def get_base_commit(source_dir: Path, cg: ConcurrencyGroup) -> str:
    """Get the current HEAD commit hash, used as the base for all agent branches."""
    result = cg.run_process_to_completion(["git", "rev-parse", "HEAD"], cwd=source_dir)
    return result.stdout.strip()
