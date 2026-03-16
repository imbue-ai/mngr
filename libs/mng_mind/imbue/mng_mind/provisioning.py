"""Mind-specific provisioning functions.

Provides provisioning for the link_skills.sh script, which symlinks
shared top-level skills into role-specific skill directories.
"""

from __future__ import annotations

import importlib.resources
import shlex
from pathlib import Path

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_llm.data_types import ProvisioningSettings
from imbue.mng_llm.provisioning import execute_with_timing
from imbue.mng_mind import resources as resources_package


def provision_link_skills_script_file(
    host: OnlineHostInterface,
    work_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Write link_skills.sh to the work directory if it doesn't already exist.

    This writes the script that symlinks shared top-level skills into
    role-specific skill directories. Only writes if the file is missing
    -- existing files are never overwritten.
    """
    target_path = work_dir / "link_skills.sh"
    check = execute_with_timing(
        host,
        f"test -f {shlex.quote(str(target_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="file check",
    )
    if check.success:
        logger.debug("link_skills.sh already exists, skipping: {}", target_path)
        return

    resources_root = importlib.resources.files(resources_package)
    script_resource = resources_root / "link_skills.sh"
    content = script_resource.read_text()

    execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(target_path.parent))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir",
    )

    with log_span("Writing link_skills.sh: {}", target_path):
        host.write_text_file(target_path, content)
