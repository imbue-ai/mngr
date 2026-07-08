"""One-shot ``mngr list`` snapshot used by ``--no-observe`` mode.

In observe-driven mode the plugin streams agents continuously from
``mngr observe --discovery-only``. In ``--no-observe`` mode (manual,
deterministic) the plugin instead invokes ``mngr list --format json`` once at
startup, parses the result, and never re-discovers. This module wraps that
subprocess call.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr_forward.data_types import ForwardAgentSnapshot
from imbue.mngr_forward.data_types import ForwardListSnapshot
from imbue.mngr_forward.errors import ForwardSubprocessError
from imbue.mngr_forward.primitives import MNGR_BINARY
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo


def mngr_list_snapshot(
    mngr_binary: str = MNGR_BINARY,
    timeout_seconds: float = 30.0,
    extra_env: dict[str, str] | None = None,
    error_behavior: ErrorBehavior = ErrorBehavior.ABORT,
) -> ForwardListSnapshot:
    """Run ``mngr list --format json`` once and parse the result.

    Returns a ``ForwardListSnapshot`` carrying every agent the user's mngr
    config currently sees, including labels and SSH info for remote hosts.
    Raises ``ForwardSubprocessError`` if the subprocess fails to spawn or
    exits non-zero. Under ``ErrorBehavior.CONTINUE`` the snapshot passes
    ``--on-error continue`` and tolerates an inaccessible/unauthenticated
    provider (exit ``EXIT_CODE_PROVIDER_INACCESSIBLE``), forwarding the agents
    the healthy providers still reported.
    """
    command: list[str] = [mngr_binary, "list", "--format", "json"]
    if error_behavior == ErrorBehavior.CONTINUE:
        command += ["--on-error", "continue"]
    cwd = Path.home()
    try:
        result = subprocess.run(  # noqa: S603 - command is fully controlled
            command,
            check=False,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout_seconds,
            env=_build_env(extra_env),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ForwardSubprocessError(f"Failed to run `{' '.join(command)}`: {e}") from e

    # Under CONTINUE, an unauthenticated/unreachable provider makes `mngr list`
    # exit EXIT_CODE_PROVIDER_INACCESSIBLE while still emitting the healthy
    # providers' agents on stdout. Treat that as a partial success and forward
    # what we can; any other nonzero exit is still a hard failure.
    is_tolerated_provider_failure = (
        error_behavior == ErrorBehavior.CONTINUE and result.returncode == EXIT_CODE_PROVIDER_INACCESSIBLE
    )
    if result.returncode != 0 and not is_tolerated_provider_failure:
        raise ForwardSubprocessError(
            f"`{' '.join(command)}` exited with code {result.returncode}: {result.stderr.strip()}"
        )
    if is_tolerated_provider_failure:
        logger.debug(
            "Tolerated inaccessible providers from `mngr list` (exit {}); forwarding agents from the healthy providers",
            result.returncode,
        )

    return _parse_snapshot(result.stdout)


def _build_env(extra_env: dict[str, str] | None) -> dict[str, str] | None:
    if extra_env is None:
        return None
    env = dict(os.environ)
    env.update(extra_env)
    return env


def _parse_snapshot(json_output: str) -> ForwardListSnapshot:
    """Parse the ``mngr list --format json`` output into a ``ForwardListSnapshot``."""
    if not json_output.strip():
        return ForwardListSnapshot()
    try:
        data = json.loads(json_output)
    except json.JSONDecodeError as e:
        raise ForwardSubprocessError(f"Could not parse `mngr list` output: {e}") from e

    raw_agents = data.get("agents", []) if isinstance(data, dict) else []
    agents: list[ForwardAgentSnapshot] = []
    for raw in raw_agents:
        if not isinstance(raw, dict):
            continue
        agent_id_str = raw.get("id")
        if agent_id_str is None:
            continue
        try:
            agent_id = AgentId(agent_id_str)
        except ValueError as e:
            logger.warning("Skipping agent with invalid id {!r}: {}", agent_id_str, e)
            continue
        ssh_info = _parse_ssh_info(raw)
        labels = _parse_labels(raw)
        agent_name = _parse_str_field(raw, "name")
        host = raw.get("host") if isinstance(raw.get("host"), dict) else {}
        host_id = _parse_str_field(host, "id")
        provider_name = _parse_str_field(host, "provider_name")
        agents.append(
            ForwardAgentSnapshot(
                agent_id=agent_id,
                ssh_info=ssh_info,
                agent_name=agent_name,
                host_id=host_id,
                provider_name=provider_name,
                labels=labels,
            )
        )

    return ForwardListSnapshot(agents=tuple(agents))


def _parse_str_field(raw: dict[str, Any] | Any, key: str) -> str:
    """Pull ``key`` out of ``raw`` as a string, defaulting to empty.

    Tolerates missing keys, non-dict containers, and non-string values so a
    partial ``mngr list --format json`` payload (e.g. an older mngr version
    that doesn't carry one of these fields) doesn't break snapshot parsing.
    """
    if not isinstance(raw, dict):
        return ""
    value = raw.get(key)
    return str(value) if value is not None else ""


def _parse_ssh_info(raw: dict[str, Any]) -> RemoteSSHInfo | None:
    host = raw.get("host")
    if not isinstance(host, dict):
        return None
    ssh = host.get("ssh")
    if not isinstance(ssh, dict):
        return None
    try:
        return RemoteSSHInfo(
            user=ssh["user"],
            host=ssh["host"],
            port=ssh["port"],
            key_path=Path(ssh["key_path"]),
        )
    except (KeyError, ValueError) as e:
        logger.warning("Could not parse SSH info: {}", e)
        return None


def _parse_labels(raw: dict[str, Any]) -> dict[str, str]:
    labels = raw.get("labels")
    if not isinstance(labels, dict):
        return {}
    return {str(k): str(v) for k, v in labels.items() if isinstance(k, str)}
