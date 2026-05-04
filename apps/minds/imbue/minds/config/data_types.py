import json
import sys
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.errors import MalformedMngrOutputError
from imbue.mngr.primitives import AgentId

DEFAULT_DESKTOP_CLIENT_HOST: Final[str] = "127.0.0.1"

DEFAULT_DESKTOP_CLIENT_PORT: Final[int] = 8420


def _resolve_mngr_binary() -> str:
    """Resolve the mngr binary path, preferring the one next to the running
    Python interpreter.

    In a uv-managed venv (dev or packaged), `mngr` sits alongside `python`
    at `<venv>/bin/mngr`. We prefer that absolute path because subprocesses
    in the packaged app inherit a PATH that does not include the venv's bin
    dir -- `os.environ['PATH']` is built by Electron and does not know about
    the internal venv. Relying on bare "mngr" + PATH lookup therefore fails
    with "No such file or directory: 'mngr'" under packaging.

    Fall back to bare "mngr" (PATH lookup) only if the sibling binary is
    absent, which preserves the dev-mode behavior where mngr may be
    installed as a uv tool outside any specific venv.
    """
    sibling = Path(sys.executable).parent / "mngr"
    if sibling.is_file():
        return str(sibling)
    return "mngr"


MNGR_BINARY: Final[str] = _resolve_mngr_binary()


class WorkspacePaths(FrozenModel):
    """Resolved filesystem paths for minds data storage."""

    data_dir: Path = Field(description="Root directory for minds data (e.g. ~/.minds)")

    @property
    def auth_dir(self) -> Path:
        """Directory for authentication data (signing key, one-time codes)."""
        return self.data_dir / "auth"

    @property
    def mngr_host_dir(self) -> Path:
        """Directory where mngr stores agent state for this minds install (e.g. ~/.minds/mngr)."""
        return self.data_dir / "mngr"

    def workspace_dir(self, agent_id: AgentId) -> Path:
        """Directory for a specific workspace's repo (e.g. ~/.minds/<agent-id>/)."""
        return self.data_dir / str(agent_id)


def parse_agents_from_mngr_output(stdout: str) -> list[dict[str, object]]:
    """Extract agent records from the first JSON object line of ``mngr list --format json`` stdout.

    Raises ``MalformedMngrOutputError`` when the first non-empty line is not a
    JSON object. stdout is reserved for JSON data; if log lines or SSH errors
    are leaking onto it, fix the underlying process rather than papering over
    it here.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            raise MalformedMngrOutputError(
                f"Expected JSON object on first non-empty mngr output line, got: {stripped[:200]!r}"
            )
        data = json.loads(stripped)
        return data["agents"]
    return []
