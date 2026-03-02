from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_claude_zygote.data_types import ClaudeZygoteSettings


def load_settings_from_host(
    host: OnlineHostInterface,
    work_dir: Path,
    changelings_dir_name: str,
) -> ClaudeZygoteSettings:
    """Load settings from .changelings/settings.toml on the host.

    Returns a ClaudeZygoteSettings with defaults for any missing values.
    If the file does not exist or cannot be parsed, returns all defaults.
    """
    settings_path = work_dir / changelings_dir_name / "settings.toml"
    quoted_path = shlex.quote(str(settings_path))

    check = host.execute_command(f"test -f {quoted_path}", timeout_seconds=15.0)
    if not check.success:
        logger.debug("No settings file at {}, using defaults", settings_path)
        return ClaudeZygoteSettings()

    with log_span("Loading settings from {}", settings_path):
        try:
            content = host.read_text_file(settings_path)
        except Exception as e:
            logger.warning("Failed to read settings file {}: {}", settings_path, e)
            return ClaudeZygoteSettings()

        try:
            raw = tomllib.loads(content)
        except tomllib.TOMLDecodeError as e:
            logger.warning("Failed to parse settings file {}: {}", settings_path, e)
            return ClaudeZygoteSettings()

        try:
            return ClaudeZygoteSettings.model_validate(raw)
        except Exception as e:
            logger.warning("Failed to validate settings from {}: {}", settings_path, e)
            return ClaudeZygoteSettings()


def provision_settings_file(
    host: OnlineHostInterface,
    work_dir: Path,
    changelings_dir_name: str,
    agent_state_dir: Path,
) -> None:
    """Copy settings.toml from the work dir to the agent state dir.

    If the source file does not exist, does nothing. The agent state dir
    location ($MNG_AGENT_STATE_DIR/settings.toml) is accessible to all
    scripts via the MNG_AGENT_STATE_DIR environment variable.
    """
    source_path = work_dir / changelings_dir_name / "settings.toml"
    dest_path = agent_state_dir / "settings.toml"
    quoted_source = shlex.quote(str(source_path))

    check = host.execute_command(f"test -f {quoted_source}", timeout_seconds=15.0)
    if not check.success:
        logger.debug("No settings file at {} to provision", source_path)
        return

    with log_span("Provisioning settings to {}", dest_path):
        try:
            content = host.read_text_file(source_path)
        except Exception as e:
            logger.warning("Failed to read settings file for provisioning: {}", e)
            return

        host.write_text_file(dest_path, content)
