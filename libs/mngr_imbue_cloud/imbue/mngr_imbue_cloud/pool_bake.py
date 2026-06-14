"""Provider-generic baking of a forever-claude-template (FCT) pool host.

This is the single place that knows how to turn a *provisioned host* (an OVH VPS,
or a lima "slice" on a bare-metal box) into a ready-to-lease pool host: run
``mngr create`` against it with the FCT bake templates, stop the services agent,
harden the container sshd, and tear down the bootstrap-created chat agent. It is
deliberately **provider-agnostic and OVH-free**: the only provider name it sees
is the opaque string on the ``mngr create`` address, and any provider-specific
steps (OVH ufw / management-key install; the slice carve) are injected by the
caller (``cli/admin.py`` for OVH, ``cli/server.py`` for slices) -- so OVH
ordering logic and FCT bake logic never mix in one module.

The bake resolves every host detail it returns from ``mngr create --format
json`` (agent id, host id, the agent SSH endpoint + on-disk key, and -- when the
provider exposes one -- the outer/management sshd port), so there is no second
``mngr list`` round-trip.
"""

import json
import os
import shlex
import sys
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.frozen_model import FrozenModel

# Constant agent name baked onto every pool host (OVH or slice). The minds-side
# adoption code (``ImbueCloudHost.create_agent_state``) keeps the bake's agent
# name verbatim, so it must match the name the user's
# ``mngr create system-services@<host>.imbue_cloud_<slug>`` lease uses --
# otherwise the user's lease ends up with an agent whose tmux session is named
# after a per-bake UUID instead of ``system-services``.
BAKED_SERVICES_AGENT_NAME: Final[str] = "system-services"

# The FCT create templates the container bake stacks: ``main`` (shared agent
# config) + ``ovh`` (build the container from the workspace Dockerfile + run
# fct-seed + runsc hardening). The ``ovh`` name is historical -- it is the
# FCT's Dockerfile-build template, not anything OVH-specific -- and it is reused
# verbatim for slices, which build the same container image.
FCT_BAKE_TEMPLATES: Final[tuple[str, ...]] = ("main", "ovh")

# Path inside the pool host's container of the FCT bootstrap's initial-chat
# sentinel. The bootstrap writes it after creating the chat agent on first boot;
# removing it (after destroying that chat agent) makes the user's first lease +
# start re-create the chat agent under the user's own workspace name.
INITIAL_CHAT_SENTINEL_PATH: Final[str] = "/code/runtime/initial_chat_created"

# 30 min: the inner ``mngr create`` builds a fresh Docker image on the host,
# which can take 10-20 min (network bound).
_MNGR_CREATE_TIMEOUT_SECONDS: Final[int] = 1800

# Manual rsync excludes layered on top of ``--filter=:- .gitignore`` for the
# monorepo -> FCT vendor/mngr sync. The filter handles ``__pycache__`` / ``.venv``
# / etc.; these two are NOT in .gitignore: ``.git`` (git's internal dir) and
# ``uv.lock`` (committed at the mngr root, but each install context regenerates
# its own).
_VENDOR_RSYNC_MANUAL_EXCLUDES: Final[tuple[str, ...]] = (".git", "uv.lock")
_GITIGNORE_RSYNC_FILTER: Final[str] = ":- .gitignore"
# How long to wait (inside the container) for the FCT bootstrap to write its
# initial-chat sentinel before giving up on the chat-agent teardown. The
# bootstrap may never create a chat agent (e.g. inference creds absent), in which
# case there is nothing to tear down and the bake proceeds.
_SENTINEL_WAIT_TIMEOUT_SECONDS: Final[int] = 480


class PoolBakeError(RuntimeError):
    """Raised when a required pool-bake step fails irrecoverably."""


class BakedPoolHost(FrozenModel):
    """The host details a successful FCT bake resolves (from ``mngr create --format json``).

    ``ssh_host`` / ``ssh_port`` / ``ssh_key_path`` are the *agent* (container) SSH
    endpoint. ``outer_ssh_port`` is the provider's separate outer/management sshd
    port when it has one (a slice's box-forwarded VM-root port); it is ``None``
    for providers whose host is reached directly (OVH).
    """

    agent_id: str = Field(description="mngr agent id of the baked services agent")
    host_id: str = Field(description="mngr host id of the baked pool host")
    host_name: str = Field(description="per-bake-unique host name")
    ssh_host: str | None = Field(default=None, description="agent SSH hostname (the VPS/box address)")
    ssh_port: int | None = Field(default=None, description="agent (container) SSH port")
    ssh_key_path: str | None = Field(default=None, description="on-disk private key path for the agent SSH endpoint")
    outer_ssh_port: int | None = Field(
        default=None, description="separate outer/management sshd port, if the provider exposes one (slice VM root)"
    )


def _stream_subprocess_line(line: str, is_stdout: bool) -> None:
    """Mirror a child-process line to our stderr in real time.

    A multi-minute pool-host bake otherwise produces no visible output until it
    completes, which makes diagnosing failures (or confirming progress) hard.
    ``mngr create --format json`` writes its one JSON object to stdout and all
    human/log output to stderr, so echoing every line here is safe -- the JSON
    is still captured in the returned ``FinishedProcess.stdout`` for parsing.
    """
    suffix = "" if line.endswith("\n") else "\n"
    sys.stderr.write(line + suffix)
    sys.stderr.flush()


def run_mngr_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = _MNGR_CREATE_TIMEOUT_SECONDS,
    is_streaming: bool = False,
    extra_env: Mapping[str, str] | None = None,
) -> FinishedProcess:
    """Run a ``mngr`` CLI command and return the result (does not raise on non-zero).

    When ``is_streaming`` the child's stdout+stderr are mirrored to our stderr
    line-by-line (and still captured). ``extra_env`` merges over ``os.environ``
    for the subprocess (used to thread OVH tags / recycle flags / slice config).
    """
    full_command = ["mngr", *args]
    logger.info("  Running: {}", " ".join(full_command))
    on_output = _stream_subprocess_line if is_streaming else None
    subprocess_env: dict[str, str] | None = None
    if extra_env:
        subprocess_env = dict(os.environ)
        subprocess_env.update(extra_env)
    cg = ConcurrencyGroup(name="pool-mngr")
    with cg:
        return cg.run_process_to_completion(
            command=full_command,
            timeout=float(timeout),
            is_checked_after=False,
            cwd=cwd,
            on_output=on_output,
            env=subprocess_env,
        )


def sync_mngr_into_template(mngr_source: Path, workspace_dir: Path) -> None:
    """Rsync the mngr monorepo into the FCT workspace's ``vendor/mngr/`` directory.

    The FCT Dockerfile COPYs ``vendor/mngr`` and builds the container's mngr from
    it, so this populates it (gitignore-filtered) before the bake -- making the
    baked container's mngr match the operator's checkout. Used identically by the
    OVH and slice bakes (both bake the same FCT image).
    """
    vendor_mngr = workspace_dir / "vendor" / "mngr"
    vendor_mngr.mkdir(parents=True, exist_ok=True)
    exclude_args: list[str] = []
    for pattern in _VENDOR_RSYNC_MANUAL_EXCLUDES:
        exclude_args.extend(["--exclude", pattern])
    command = [
        "rsync",
        "-a",
        "--delete",
        f"--filter={_GITIGNORE_RSYNC_FILTER}",
        *exclude_args,
        f"{mngr_source}/",
        f"{vendor_mngr}/",
    ]
    logger.info("Syncing mngr source into {}", vendor_mngr)
    cg = ConcurrencyGroup(name="rsync-vendor")
    with cg:
        result = cg.run_process_to_completion(command=command, is_checked_after=False, timeout=120.0)
    if result.returncode != 0:
        raise PoolBakeError(
            f"rsync of {mngr_source} into {vendor_mngr} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def build_pool_create_command(
    *,
    provider_instance: str,
    host_name: str,
    attributes_json: str,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Render the ``mngr create`` argv for an FCT pool-host bake.

    Common across OVH + slices: a new host running the ``system-services`` agent,
    baked with the FCT templates, emitting ``--format json`` so the caller can
    resolve host details without a ``mngr list`` round-trip. Provider-specific
    args (OVH ``-b --ovh-datacenter=...`` / recycle; slice ``-S`` sizing + box
    config) are appended verbatim via ``extra_args``.
    """
    address = f"{BAKED_SERVICES_AGENT_NAME}@{host_name}.{provider_instance}"
    command = [
        "create",
        address,
        "--new-host",
        "--no-connect",
        "--idle-mode",
        "disabled",
    ]
    for template in FCT_BAKE_TEMPLATES:
        command.extend(["--template", template])
    command.extend(
        [
            "--format",
            "json",
            "--label",
            f"workspace={BAKED_SERVICES_AGENT_NAME}",
            "--label",
            "user_created=true",
            "--label",
            "is_primary=true",
            "--label",
            f"pool_attributes={attributes_json}",
            "--host-env",
            "MNGR_HOST_DIR=/mngr",
        ]
    )
    command.extend(extra_args)
    return command


def parse_baked_host(stdout: str, *, host_name: str) -> BakedPoolHost:
    """Parse the ``mngr create --format json`` object from a bake's stdout.

    ``--format json`` writes exactly one JSON object to stdout (logs go to
    stderr), so the last ``{...}`` line is the result. A malformed candidate or a
    payload missing the guaranteed ``host_id`` raises ``PoolBakeError`` (never
    silently swallowed).
    """
    candidates = [
        line.strip() for line in stdout.splitlines() if line.strip().startswith("{") and line.strip().endswith("}")
    ]
    if not candidates:
        raise PoolBakeError(f"no `mngr create --format json` object found in bake output: {stdout[-500:]!r}")
    try:
        parsed = json.loads(candidates[-1])
    except json.JSONDecodeError as exc:
        raise PoolBakeError(f"`mngr create --format json` output was not valid JSON: {candidates[-1]!r}") from exc
    if not isinstance(parsed, dict) or "host_id" not in parsed:
        raise PoolBakeError(f"`mngr create --format json` output missing host_id: {parsed!r}")
    ssh_port = parsed.get("ssh_port")
    outer_ssh_port = parsed.get("outer_ssh_port")
    return BakedPoolHost(
        agent_id=str(parsed["agent_id"]),
        host_id=str(parsed["host_id"]),
        host_name=str(parsed.get("host_name", host_name)),
        ssh_host=parsed.get("ssh_host"),
        ssh_port=int(ssh_port) if ssh_port is not None else None,
        ssh_key_path=parsed.get("ssh_key_path"),
        outer_ssh_port=int(outer_ssh_port) if outer_ssh_port is not None else None,
    )


def _ensure_container_sshd_robust(full_address: str) -> None:
    """Bump the container sshd's pre-auth limits so the lease/claim flow doesn't lose connections.

    The default ``MaxStartups=10:30:100`` caps the pre-auth queue tightly, and the
    imbue_cloud lease + claim flow plus parallel ``mngr observe`` discovery
    routinely exceeds it and drops connections mid-rsync. Packed into one
    shell-quoted ``mngr exec`` COMMAND positional.
    """
    sshd_command = shlex.join(["/usr/sbin/sshd", "-o", "MaxSessions=100", "-o", "MaxStartups=100:30:200"])
    run_mngr_command(["exec", full_address, sshd_command], timeout=30)


def _teardown_bootstrap_chat_agent(full_address: str, *, host_name: str, sentinel_timeout_seconds: int) -> None:
    """Tear down the FCT-bootstrap-created chat agent so the user's first lease gets a fresh one.

    During the bake the services agent booted and the FCT bootstrap created an
    initial chat agent named after the bake's ``host_name`` -- the wrong name for
    the user's eventual workspace, and the bootstrap won't recreate it on later
    starts (it left a sentinel). Wait (inside the container) for that sentinel,
    then destroy the chat agent and remove the sentinel so the user's first start
    recreates the chat agent under their own host name.

    If no sentinel appears within the timeout, the bootstrap never created a chat
    agent (e.g. inference creds absent) -- there is nothing to tear down, so this
    warns and returns. When the sentinel *is* present the destroy must succeed: a
    destroy error almost always signals a vendored-mngr / FCT-template skew, and
    shipping a pool host whose bootstrap state we don't understand has bitten us
    before, so we fail the bake rather than land a half-known host in the pool.
    """
    sentinel = shlex.quote(INITIAL_CHAT_SENTINEL_PATH)
    wait_inner = f"until test -f {sentinel}; do sleep 5; done"
    wait_command = shlex.join(
        ["bash", "-lc", f"timeout {int(sentinel_timeout_seconds)} bash -c {shlex.quote(wait_inner)}"]
    )
    wait_result = run_mngr_command(["exec", full_address, wait_command], timeout=sentinel_timeout_seconds + 60)
    if wait_result.returncode != 0:
        logger.warning("No initial-chat sentinel appeared for {}; skipping chat-agent teardown", host_name)
        return
    logger.info("  Destroying bootstrap-created chat agent: {}", host_name)
    # ``mngr exec`` parses ``AGENTS... COMMAND``; pack the whole remote command
    # into one shell-quoted positional so it lands as a single COMMAND.
    chat_destroy_cmd = shlex.join(["mngr", "destroy", host_name, "--force"])
    chat_destroy = run_mngr_command(["exec", full_address, chat_destroy_cmd], timeout=120)
    if chat_destroy.returncode != 0:
        raise PoolBakeError(
            f"destroying bootstrap chat agent {host_name!r} via "
            f"`mngr exec {full_address} {chat_destroy_cmd!r}` failed "
            f"(exit {chat_destroy.returncode}): {chat_destroy.stderr.strip()}"
        )
    logger.info("  Removing initial-chat sentinel: {}", INITIAL_CHAT_SENTINEL_PATH)
    sentinel_rm_cmd = shlex.join(["rm", "-f", INITIAL_CHAT_SENTINEL_PATH])
    sentinel_rm = run_mngr_command(["exec", full_address, sentinel_rm_cmd], timeout=30)
    if sentinel_rm.returncode != 0:
        raise PoolBakeError(
            f"removing initial-chat sentinel {INITIAL_CHAT_SENTINEL_PATH!r} failed: {sentinel_rm.stderr.strip()}"
        )


def bake_pool_host(
    *,
    provider_instance: str,
    host_name: str,
    attributes: Mapping[str, Any],
    workspace_dir: Path,
    extra_create_args: Sequence[str] = (),
    extra_create_env: Mapping[str, str] | None = None,
    on_failure_after_create: Callable[[BakedPoolHost], None] | None = None,
    sentinel_timeout_seconds: int = _SENTINEL_WAIT_TIMEOUT_SECONDS,
) -> BakedPoolHost:
    """Bake one FCT pool host on a provisioned host and return its resolved details.

    Sequence (identical for OVH + slices): ``mngr create`` (with the FCT
    templates + ``--format json``) -> parse host details -> stop the services
    agent -> harden the container sshd -> tear down the bootstrap chat agent. The
    caller then runs any provider-specific host hardening (OVH installs ufw + the
    management key; slices need none, having authorized the pool key at carve time)
    and inserts the provider-specific ``pool_hosts`` row from the returned
    :class:`BakedPoolHost`.

    Failure handling: ``mngr create`` failing means the host was never fully
    provisioned (the provider rolls back its own VM/VPS), so this just raises. But
    if a *post-create* step fails, the host exists and would leak -- so
    ``on_failure_after_create`` (if given) is invoked with the resolved
    :class:`BakedPoolHost` to tear it down before the :class:`PoolBakeError`
    propagates. That callback must be best-effort (it must not raise; it runs on an
    already-failing path). Callers whose later hardening/insert can also fail
    should run the same rollback around those steps, keyed on the returned
    ``host_id``.

    Raises :class:`PoolBakeError` on any required step failing.
    """
    full_address = f"{BAKED_SERVICES_AGENT_NAME}@{host_name}.{provider_instance}"
    attributes_json = json.dumps(dict(attributes))
    create_command = build_pool_create_command(
        provider_instance=provider_instance,
        host_name=host_name,
        attributes_json=attributes_json,
        extra_args=extra_create_args,
    )
    create_result = run_mngr_command(create_command, cwd=workspace_dir, is_streaming=True, extra_env=extra_create_env)
    if create_result.returncode != 0:
        raise PoolBakeError(
            f"`mngr create {full_address}` failed (exit {create_result.returncode}): {create_result.stderr.strip()}"
        )
    baked = parse_baked_host(create_result.stdout, host_name=host_name)
    logger.info("  Baked services agent {} on host {}", baked.agent_id, baked.host_id)

    # The host now exists; any failure past this point must tear it down (via the
    # caller's rollback) so it does not leak its slot / forwarded ports.
    try:
        stop_result = run_mngr_command(["stop", full_address], timeout=120)
        if stop_result.returncode != 0:
            raise PoolBakeError(
                f"`mngr stop {full_address}` failed (exit {stop_result.returncode}): {stop_result.stderr.strip()}"
            )
        _ensure_container_sshd_robust(full_address)
        _teardown_bootstrap_chat_agent(
            full_address, host_name=host_name, sentinel_timeout_seconds=sentinel_timeout_seconds
        )
    except PoolBakeError:
        if on_failure_after_create is not None:
            on_failure_after_create(baked)
        raise
    return baked
