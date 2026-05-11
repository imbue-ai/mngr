"""Thin wrapper around the sbx CLI.

All sbx subprocess interactions live here so the rest of the plugin can pretend
sbx is a typed Python interface. The functions raise the typed errors defined
in ``imbue.mngr_sbx.errors`` at the boundary.
"""

import json
import shutil
from collections.abc import Sequence
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_sbx.errors import SbxCommandError
from imbue.mngr_sbx.errors import SbxNotAuthorizedError
from imbue.mngr_sbx.errors import SbxNotInstalledError

# sbx surfaces a Docker auth failure with one of several human-readable
# strings depending on which subcommand was invoked. Match all of them so we
# can translate to a typed error.
_NOT_AUTHENTICATED_MARKERS: Final[tuple[str, ...]] = (
    "Not authenticated to Docker",
    "You are not authenticated to Docker",
    "user is not authenticated to Docker",
    "Sign in with: sbx login",
    "please sign in to Docker",
)


def _log_sbx_output(line: str, is_stdout: bool) -> None:
    stripped = line.strip()
    if stripped:
        logger.log(LogLevel.BUILD.value, "{}", stripped, source="sbx")


def check_sbx_installed(provider_name: ProviderInstanceName) -> None:
    """Raise SbxNotInstalledError if the sbx CLI is not on PATH."""
    if shutil.which("sbx") is None:
        raise SbxNotInstalledError(provider_name)


def _raise_if_not_authenticated(
    provider_name: ProviderInstanceName,
    command: str,
    stderr: str,
) -> None:
    """If stderr indicates a Docker auth failure, raise SbxNotAuthorizedError."""
    for marker in _NOT_AUTHENTICATED_MARKERS:
        if marker in stderr:
            raise SbxNotAuthorizedError(provider_name)


def check_sbx_authenticated(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    timeout_seconds: float,
) -> None:
    """Probe sbx for usable Docker credentials by calling ``sbx ls``.

    ``sbx ls`` is the cheapest authenticated command. If credentials are
    missing, it prints "Not authenticated to Docker" to stderr and exits
    non-zero; we translate that to ``SbxNotAuthorizedError``. Any other
    non-zero exit is surfaced as ``SbxCommandError``.
    """
    check_sbx_installed(provider_name)
    with log_span("Probing sbx authentication"):
        result = cg.run_process_to_completion(
            ["sbx", "ls", "--quiet"],
            timeout=timeout_seconds,
            is_checked_after=False,
        )
    if result.returncode == 0:
        return
    _raise_if_not_authenticated(provider_name, "ls", result.stderr + result.stdout)
    raise SbxCommandError("ls", result.returncode, result.stderr or result.stdout)


class SbxSandboxInfo(FrozenModel):
    """One row of ``sbx ls --json`` output, mapped to the fields we care about."""

    name: str = Field(description="Sandbox name")
    agent: str = Field(description="Agent type the sandbox was created for")
    status: str = Field(description="sbx-reported status (e.g. 'running', 'stopped')")
    workspaces: tuple[str, ...] = Field(
        default=(),
        description="Workspace paths mounted into the sandbox (sbx returns these as a list)",
    )
    raw: dict[str, Any] = Field(default_factory=dict, description="Original JSON record from sbx")


def sbx_list(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    timeout: float = 30.0,
) -> list[SbxSandboxInfo]:
    """Run ``sbx ls --json`` and parse the result into typed records.

    sbx 0.28.x emits ``{"sandboxes": [...]}``. Older builds emit a top-level
    list, and some output mode produces JSONL; handle all three for forward/
    backward compatibility.
    """
    with log_span("Running sbx ls --json"):
        result = cg.run_process_to_completion(
            ["sbx", "ls", "--json"],
            timeout=timeout,
            is_checked_after=False,
        )
    if result.returncode != 0:
        combined = result.stderr + result.stdout
        _raise_if_not_authenticated(provider_name, "ls", combined)
        raise SbxCommandError("ls", result.returncode, combined)

    output = result.stdout.strip()
    if not output:
        return []

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse 'sbx ls --json' output: {}", e)
        return []

    records = _extract_sandbox_records(parsed, output)

    sandboxes: list[SbxSandboxInfo] = []
    for record in records:
        name_value = record.get("name") or record.get("Name")
        if not isinstance(name_value, str):
            continue
        agent_value = record.get("agent") or record.get("Agent") or ""
        status_value = record.get("status") or record.get("Status") or ""
        workspaces_value = record.get("workspaces") or record.get("Workspaces") or ()
        workspaces: tuple[str, ...]
        if isinstance(workspaces_value, list):
            workspaces = tuple(w for w in workspaces_value if isinstance(w, str))
        elif isinstance(workspaces_value, str):
            workspaces = (workspaces_value,)
        else:
            workspaces = ()
        sandboxes.append(
            SbxSandboxInfo(
                name=name_value,
                agent=str(agent_value),
                status=str(status_value),
                workspaces=workspaces,
                raw=record,
            )
        )
    return sandboxes


def _extract_sandbox_records(parsed: Any, raw_output: str) -> list[dict[str, Any]]:
    """Normalize the various shapes ``sbx ls --json`` may produce into a flat list of dicts."""
    if isinstance(parsed, dict):
        nested = parsed.get("sandboxes")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    # Fall back: try line-delimited JSON.
    records: list[dict[str, Any]] = []
    for line in raw_output.splitlines():
        stripped_line = line.strip()
        if not stripped_line:
            continue
        try:
            obj = json.loads(stripped_line)
        except json.JSONDecodeError as e:
            logger.warning("Skipping invalid JSONL line from 'sbx ls --json': {}", e)
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def sbx_create(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    name: str,
    agent_type: str,
    workspace_path: str,
    extra_workspaces: Sequence[str] = (),
    template: str | None = None,
    cpus: int = 0,
    memory: str | None = None,
    extra_args: Sequence[str] = (),
    timeout: float = 600.0,
) -> None:
    """Run ``sbx create <agent_type> <workspace> [extra_workspaces...] --name <name> [...]``."""
    cmd: list[str] = ["sbx", "create", agent_type, workspace_path, *extra_workspaces, "--name", name]
    if template is not None:
        cmd.extend(["--template", template])
    if cpus > 0:
        cmd.extend(["--cpus", str(cpus)])
    if memory is not None:
        cmd.extend(["--memory", memory])
    cmd.extend(extra_args)

    with log_span("Running sbx create: {}", name):
        result = cg.run_process_to_completion(
            cmd,
            timeout=timeout,
            on_output=_log_sbx_output,
            is_checked_after=False,
        )
    if result.returncode != 0:
        combined = result.stderr + result.stdout
        _raise_if_not_authenticated(provider_name, "create", combined)
        raise SbxCommandError("create", result.returncode, combined)


def sbx_exec(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    name: str,
    command: Sequence[str],
    user: str | None = None,
    workdir: str | None = None,
    detach: bool = False,
    timeout: float = 120.0,
) -> tuple[int | None, str, str]:
    """Run ``sbx exec <name> <command...>``.

    Returns ``(returncode, stdout, stderr)``. ``returncode`` is ``None`` when the
    sbx process was killed before reporting an exit code. Does not raise on
    non-zero exit -- callers decide whether to treat that as an error.
    """
    cmd: list[str] = ["sbx", "exec"]
    if user is not None:
        cmd.extend(["-u", user])
    if workdir is not None:
        cmd.extend(["-w", workdir])
    if detach:
        cmd.append("-d")
    cmd.append(name)
    cmd.extend(command)

    with log_span("Running sbx exec on {}", name):
        result = cg.run_process_to_completion(cmd, timeout=timeout, is_checked_after=False)
    if result.returncode != 0:
        _raise_if_not_authenticated(provider_name, "exec", result.stderr + result.stdout)
    return result.returncode, result.stdout, result.stderr


def sbx_stop(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    name: str,
    timeout: float = 60.0,
) -> None:
    """Run ``sbx stop <name>``."""
    with log_span("Running sbx stop: {}", name):
        result = cg.run_process_to_completion(["sbx", "stop", name], timeout=timeout, is_checked_after=False)
    if result.returncode != 0:
        combined = result.stderr + result.stdout
        _raise_if_not_authenticated(provider_name, "stop", combined)
        raise SbxCommandError("stop", result.returncode, combined)


def sbx_rm(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    name: str,
    force: bool = True,
    timeout: float = 60.0,
) -> None:
    """Run ``sbx rm [--force] <name>``."""
    cmd: list[str] = ["sbx", "rm"]
    if force:
        cmd.append("--force")
    cmd.append(name)
    with log_span("Running sbx rm: {}", name):
        result = cg.run_process_to_completion(cmd, timeout=timeout, is_checked_after=False)
    if result.returncode != 0:
        combined = result.stderr + result.stdout
        _raise_if_not_authenticated(provider_name, "rm", combined)
        raise SbxCommandError("rm", result.returncode, combined)


class SbxPortBinding(FrozenModel):
    """A single host->sandbox port mapping returned by ``sbx ports``."""

    sandbox_port: int = Field(description="Port number inside the sandbox")
    host_ip: str = Field(description="Host-side IP address")
    host_port: int = Field(description="Port number on the host")
    protocol: str = Field(default="tcp", description="Protocol (tcp or udp)")


def sbx_publish_port(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    name: str,
    sandbox_port: int,
    host_port: int | None = None,
    timeout: float = 30.0,
) -> SbxPortBinding:
    """Publish a sandbox port to the host. Returns the resulting binding.

    When ``host_port`` is None, sbx allocates an ephemeral port.
    """
    spec = f"{host_port}:{sandbox_port}" if host_port is not None else str(sandbox_port)
    cmd = ["sbx", "ports", name, "--publish", spec]
    with log_span("Publishing sbx port {} on {}", spec, name):
        result = cg.run_process_to_completion(cmd, timeout=timeout, is_checked_after=False)
    if result.returncode != 0:
        combined = result.stderr + result.stdout
        _raise_if_not_authenticated(provider_name, "ports", combined)
        raise SbxCommandError("ports", result.returncode, combined)

    bindings = _parse_port_listing(result.stdout)
    for binding in bindings:
        if binding.sandbox_port == sandbox_port:
            return binding
    raise SbxCommandError(
        "ports",
        result.returncode,
        f"Could not find published port {sandbox_port} in sbx ports output: {result.stdout!r}",
    )


def sbx_list_ports(
    cg: ConcurrencyGroup,
    provider_name: ProviderInstanceName,
    name: str,
    timeout: float = 30.0,
) -> list[SbxPortBinding]:
    """Run ``sbx ports <name>`` and parse the human-readable listing."""
    with log_span("Listing sbx ports on {}", name):
        result = cg.run_process_to_completion(["sbx", "ports", name], timeout=timeout, is_checked_after=False)
    if result.returncode != 0:
        combined = result.stderr + result.stdout
        _raise_if_not_authenticated(provider_name, "ports", combined)
        raise SbxCommandError("ports", result.returncode, combined)
    return _parse_port_listing(result.stdout)


def _parse_port_listing(stdout: str) -> list[SbxPortBinding]:
    """Parse ``sbx ports`` output.

    sbx 0.28.x emits two distinct shapes:

    - ``sbx ports <name> --publish <port>`` returns a single line:
      ``Published 127.0.0.1:49153 -> 22/tcp``
    - ``sbx ports <name>`` (no flag) returns a table with a header row
      and columns ``HOST IP``, ``HOST PORT``, ``SANDBOX PORT``, ``PROTOCOL``.

    Older arrow-only forms (``22/tcp -> 127.0.0.1:32769`` and
    ``127.0.0.1:32769->22/tcp``) are also accepted so a future sbx revision
    that swaps formats doesn't silently break us. Lines we don't recognize
    are skipped with a debug log so the rest of the output is still usable.
    """
    bindings: list[SbxPortBinding] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip the table header from ``sbx ports <name>``.
        if line.upper().startswith("HOST IP"):
            continue
        if line.lower().startswith(("no ports", "name", "port")):
            continue
        parsed = _parse_port_line(line)
        if parsed is None:
            logger.debug("Skipping unrecognized sbx ports line: {}", line)
            continue
        bindings.append(parsed)
    return bindings


def _parse_port_line(line: str) -> SbxPortBinding | None:
    """Parse a single sbx ports listing line into an SbxPortBinding."""
    # ``Published 127.0.0.1:49153 -> 22/tcp`` (from sbx ports --publish)
    publish_prefix = "Published "
    if line.startswith(publish_prefix):
        return _parse_port_line(line[len(publish_prefix) :].strip())

    # Form: "<sandbox_port>/<proto> -> <host_ip>:<host_port>"
    if " -> " in line:
        left_side, right_side = line.split(" -> ", 1)
        return _build_binding_from_arrow_sides(left_side.strip(), right_side.strip())

    # Form: "<host_ip>:<host_port>-><sandbox_port>/<proto>" (no spaces, Docker default)
    if "->" in line:
        left_side, right_side = line.split("->", 1)
        return _build_binding_from_arrow_sides(left_side.strip(), right_side.strip())

    # Whitespace-separated table row: "HOST_IP  HOST_PORT  SANDBOX_PORT  PROTOCOL"
    columns = line.split()
    if len(columns) >= 4:
        host_ip = columns[0]
        try:
            host_port = int(columns[1])
            sandbox_port = int(columns[2])
        except ValueError:
            return None
        protocol = columns[3] or "tcp"
        return SbxPortBinding(
            sandbox_port=sandbox_port,
            host_ip=host_ip,
            host_port=host_port,
            protocol=protocol,
        )

    return None


def _build_binding_from_arrow_sides(left: str, right: str) -> SbxPortBinding | None:
    """Build an SbxPortBinding from the two sides of an arrow, in either orientation."""
    # Try (sandbox -> host) orientation first.
    sandbox_value, proto = _split_port_and_protocol(left)
    host_ip, host_port_value = _split_host_address(right)
    if sandbox_value is not None and host_ip is not None and host_port_value is not None:
        return SbxPortBinding(
            sandbox_port=sandbox_value,
            host_ip=host_ip,
            host_port=host_port_value,
            protocol=proto or "tcp",
        )
    # Try (host -> sandbox) orientation.
    host_ip, host_port_value = _split_host_address(left)
    sandbox_value, proto = _split_port_and_protocol(right)
    if sandbox_value is not None and host_ip is not None and host_port_value is not None:
        return SbxPortBinding(
            sandbox_port=sandbox_value,
            host_ip=host_ip,
            host_port=host_port_value,
            protocol=proto or "tcp",
        )
    return None


def _split_port_and_protocol(value: str) -> tuple[int | None, str | None]:
    """Split '<port>/<proto>' into (port, protocol)."""
    if "/" in value:
        port_part, proto_part = value.split("/", 1)
        try:
            return int(port_part.strip()), proto_part.strip() or "tcp"
        except ValueError:
            return None, None
    try:
        return int(value.strip()), "tcp"
    except ValueError:
        return None, None


def _split_host_address(value: str) -> tuple[str | None, int | None]:
    """Split '<ip>:<port>' into (ip, port). Accepts bare ports for shorthand listings."""
    if ":" in value:
        host_part, port_part = value.rsplit(":", 1)
        try:
            return host_part.strip() or "127.0.0.1", int(port_part.strip())
        except ValueError:
            return None, None
    try:
        return "127.0.0.1", int(value.strip())
    except ValueError:
        return None, None
