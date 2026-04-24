"""On-disk persistence for per-agent Latchkey gateway records.

The minds desktop client spawns ``latchkey gateway`` subprocesses that must
outlive the desktop client itself (so agents running in containers/VMs can
keep making authenticated API calls across desktop-client restarts). We
persist ``{agent_id -> {host, port, pid, started_at}}`` so the next
desktop-client launch can identify which gateways are still alive and
belong to which agents.

Files live at ``{data_dir}/agents/{agent_id}/latchkey_gateway.json``,
matching the existing ``tunnel_token`` / ``minds_api_url`` pattern.
"""

from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId

_RECORD_FILENAME = "latchkey_gateway.json"
_AGENTS_DIR_NAME = "agents"


class LatchkeyGatewayInfo(FrozenModel):
    """Metadata identifying a running Latchkey gateway subprocess.

    Used both as the return type of manager methods and as the on-disk
    representation (one file per agent).
    """

    agent_id: AgentId = Field(description="The agent this gateway is dedicated to")
    host: str = Field(description="Host the gateway is listening on (typically 127.0.0.1)")
    port: int = Field(description="Port the gateway is listening on")
    pid: int = Field(description="PID of the ``latchkey gateway`` process")
    started_at: datetime = Field(description="UTC timestamp when the gateway was started")


def _agent_info_path(data_dir: Path, agent_id: AgentId) -> Path:
    return data_dir / _AGENTS_DIR_NAME / str(agent_id) / _RECORD_FILENAME


def save_gateway_info(data_dir: Path, info: LatchkeyGatewayInfo) -> None:
    """Write a gateway info record for an agent, overwriting any existing one."""
    path = _agent_info_path(data_dir, info.agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(info.model_dump_json(indent=2))
    logger.debug("Saved latchkey gateway info for agent {} at {}", info.agent_id, path)


def load_gateway_info(data_dir: Path, agent_id: AgentId) -> LatchkeyGatewayInfo | None:
    """Read the gateway info for an agent, or None if missing or malformed."""
    path = _agent_info_path(data_dir, agent_id)
    if not path.is_file():
        return None
    try:
        raw = path.read_text()
    except OSError as e:
        logger.warning("Failed to read latchkey gateway info at {}: {}", path, e)
        return None
    try:
        return LatchkeyGatewayInfo.model_validate_json(raw)
    except ValueError as e:
        logger.warning("Malformed latchkey gateway info at {}: {}", path, e)
        return None


def delete_gateway_info(data_dir: Path, agent_id: AgentId) -> None:
    """Remove the stored gateway info for an agent (no-op if absent)."""
    path = _agent_info_path(data_dir, agent_id)
    if path.is_file():
        try:
            path.unlink()
            logger.debug("Deleted latchkey gateway info for agent {}", agent_id)
        except OSError as e:
            logger.warning("Failed to delete latchkey gateway info at {}: {}", path, e)


def list_gateway_infos(data_dir: Path) -> list[LatchkeyGatewayInfo]:
    """Return all persisted gateway infos under ``data_dir``.

    Malformed records are logged and skipped rather than aborting the scan.
    """
    agents_dir = data_dir / _AGENTS_DIR_NAME
    if not agents_dir.is_dir():
        return []
    infos: list[LatchkeyGatewayInfo] = []
    for entry in agents_dir.iterdir():
        if not entry.is_dir():
            continue
        path = entry / _RECORD_FILENAME
        if not path.is_file():
            continue
        try:
            info = LatchkeyGatewayInfo.model_validate_json(path.read_text())
        except (OSError, ValueError) as e:
            logger.warning("Skipping malformed latchkey gateway info at {}: {}", path, e)
            continue
        infos.append(info)
    return infos


def gateway_log_path(data_dir: Path, agent_id: AgentId) -> Path:
    """Return the log file path for an agent's gateway subprocess."""
    return data_dir / _AGENTS_DIR_NAME / str(agent_id) / "latchkey_gateway.log"


def ensure_browser_log_path(data_dir: Path) -> Path:
    """Return the log file path for the one-shot ``latchkey ensure-browser`` subprocess.

    Not agent-scoped: ``ensure-browser`` is a minds-wide one-time setup
    step that configures a shared Playwright/Chromium browser for the
    latchkey credential directory, run at most once per minds session.
    """
    return data_dir / "latchkey_ensure_browser.log"
