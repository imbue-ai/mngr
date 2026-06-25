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
# config) + ``pool_host`` (build the container from the workspace Dockerfile + run
# fct-seed + runsc hardening). ``pool_host`` is provider-agnostic -- it carries
# only the FCT build recipe, not a provider -- so the same template bakes OVH
# VPSes and lima slices alike; the provider is selected entirely by the create
# address (``@host.ovh`` vs ``@host.imbue_cloud_slice``), matching how the
# ``aws`` / ``imbue_cloud`` templates already work.
FCT_BAKE_TEMPLATES: Final[tuple[str, ...]] = ("main", "pool_host")

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
# Exit code GNU ``timeout`` returns when it kills the wrapped command on timeout.
_COMMAND_TIMEOUT_EXIT_CODE: Final[int] = 124

# The FCT ``deferred-install`` service writes this marker on success; the bake waits
# for it (see ``wait_for_deferred_install``) before stopping the services agent.
_DEFERRED_INSTALL_MARKER: Final[str] = "/var/lib/minds/deferred-install/done.playwright"
# Cap on how long the bake blocks for the deferred install (heavy apt + browser
# download) to finish; on timeout the bake proceeds and the install retries on lease.
_DEFERRED_INSTALL_WAIT_TIMEOUT_SECONDS: Final[int] = 900


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
    ssh_user: str = Field(default="root", description="agent (container) SSH user")
    ssh_host: str | None = Field(default=None, description="agent SSH hostname (the VPS/box address)")
    ssh_port: int | None = Field(default=None, description="agent (container) SSH port")
    ssh_key_path: str | None = Field(default=None, description="on-disk private key path for the agent SSH endpoint")
    outer_ssh_port: int | None = Field(
        default=None, description="separate outer/management sshd port, if the provider exposes one (slice VM root)"
    )
    outer_host_public_key: str | None = Field(
        default=None, description="the VPS/VM-root sshd host public key (baked, deterministic), to pin"
    )
    container_host_public_key: str | None = Field(
        default=None, description="the container sshd host public key (baked, deterministic), to pin"
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
        ssh_user=str(parsed.get("ssh_user", "root")),
        ssh_host=parsed.get("ssh_host"),
        ssh_port=int(ssh_port) if ssh_port is not None else None,
        ssh_key_path=parsed.get("ssh_key_path"),
        outer_ssh_port=int(outer_ssh_port) if outer_ssh_port is not None else None,
        outer_host_public_key=parsed.get("outer_host_public_key"),
        container_host_public_key=parsed.get("container_host_public_key"),
    )


# A function that runs a shell command *inside the baked pool host's container*
# and returns ``(returncode, stdout, stderr)``. The transport is provider-specific
# and supplied by the caller, because reaching a baked host differs by provider:
# OVH uses ``mngr exec`` (the agent is resolvable in the operator's mngr state);
# a slice's per-host sshd port lives only in the create process's memory, so a
# fresh ``mngr`` can't resolve it -- the slice caller instead SSHes straight to the
# create-reported forwarded port. The runner receives the :class:`BakedPoolHost`
# (so the slice transport can read that endpoint) plus a label + timeout, and is
# expected to execute the command via a login shell (so ``uv``/``mngr`` are on
# PATH inside the FCT container).
ContainerCommandRunner = Callable[[BakedPoolHost, str, str, float], tuple[int | None, str, str]]


def bake_pool_host(
    *,
    provider_instance: str,
    host_name: str,
    attributes: Mapping[str, Any],
    workspace_dir: Path,
    extra_create_args: Sequence[str] = (),
    extra_create_env: Mapping[str, str] | None = None,
    mngr_create_timeout_seconds: int = _MNGR_CREATE_TIMEOUT_SECONDS,
) -> BakedPoolHost:
    """Run ``mngr create`` for one FCT pool host and return its resolved details.

    Shared by OVH + slices: builds the FCT create command (templates + labels +
    ``--format json``), runs it (with the provider-specific ``extra_create_args`` /
    ``extra_create_env``), and parses the create JSON into a :class:`BakedPoolHost`.
    The provider-specific post-create work -- stopping the services agent (OVH),
    container sshd-hardening + chat-agent teardown (both, via
    :func:`finalize_baked_pool_host`), host hardening (OVH ufw + management key),
    the ``pool_hosts`` insert, and any rollback -- is the caller's, since the
    transport to reach the baked host and the rollback differ by provider.

    A failed ``mngr create`` means the host was never fully provisioned (the
    provider rolls back its own VM/VPS), so this just raises. Raises
    :class:`PoolBakeError` on create failure or unparseable output.
    """
    full_address = f"{BAKED_SERVICES_AGENT_NAME}@{host_name}.{provider_instance}"
    attributes_json = json.dumps(dict(attributes))
    create_command = build_pool_create_command(
        provider_instance=provider_instance,
        host_name=host_name,
        attributes_json=attributes_json,
        extra_args=extra_create_args,
    )
    create_result = run_mngr_command(
        create_command,
        cwd=workspace_dir,
        timeout=mngr_create_timeout_seconds,
        is_streaming=True,
        extra_env=extra_create_env,
    )
    if create_result.returncode != 0:
        raise PoolBakeError(
            f"`mngr create {full_address}` failed (exit {create_result.returncode}): {create_result.stderr.strip()}"
        )
    baked = parse_baked_host(create_result.stdout, host_name=host_name)
    logger.info("  Baked services agent {} on host {}", baked.agent_id, baked.host_id)
    return baked


def wait_for_deferred_install(
    run_in_container: ContainerCommandRunner,
    baked: BakedPoolHost,
    *,
    host_name: str,
    timeout_seconds: int = _DEFERRED_INSTALL_WAIT_TIMEOUT_SECONDS,
) -> None:
    """Wait for the FCT ``deferred-install`` service to finish before the caller stops the services agent.

    The deferred-install service kicks off a heavy apt + Playwright/Chromium install at agent boot.
    Stopping the services agent mid-apt kills it, leaving dpkg half-unpacked (reinst-required) -- so the
    install only completes after a repair on the post-lease retry. Calling this right before the stop
    avoids that interruption. Both backends must call it before their respective ``mngr stop`` (OVH stops
    before ``finalize_baked_pool_host``, slices after, so this is a standalone step rather than part of
    finalize). Runs inside the container via the caller-supplied transport.

    Blocks until either the success marker exists OR the ``deferred_install.sh`` process is no longer
    running -- the latter so a not-yet-started or already-finished/failed install does not block us (it
    runs or retries cleanly post-lease). Best-effort with a cap: on timeout we log and proceed.
    """
    # The bracket in '[d]eferred_install.sh' is the classic self-match guard: the regex still matches the
    # real "bash scripts/deferred_install.sh" process, but this wait command's own command line contains
    # "[d]eferred_install.sh" (not the literal), so pgrep does not match itself into an infinite loop.
    poll = (
        f"until test -f {_DEFERRED_INSTALL_MARKER} || "
        f"! pgrep -f '[d]eferred_install.sh' >/dev/null 2>&1; do sleep 5; done"
    )
    wait_command = f"timeout {int(timeout_seconds)} bash -c {shlex.quote(poll)}"
    rc, _out, err = run_in_container(baked, "deferred-install-wait", wait_command, float(timeout_seconds + 60))
    if rc == 0:
        # The install finished (or had not started / had already exited); safe to stop.
        pass
    elif rc == _COMMAND_TIMEOUT_EXIT_CODE:
        logger.warning(
            "deferred-install on {} did not finish within {}s; proceeding (it retries on first lease)",
            host_name,
            timeout_seconds,
        )
    else:
        logger.warning("Could not wait for deferred-install on {} (exit {}): {}", host_name, rc, err.strip())


def finalize_baked_pool_host(
    run_in_container: ContainerCommandRunner,
    baked: BakedPoolHost,
    *,
    host_name: str,
    sentinel_timeout_seconds: int = _SENTINEL_WAIT_TIMEOUT_SECONDS,
) -> None:
    """Harden the container sshd and tear down the FCT bootstrap chat agent (shared FCT post-bake).

    Runs entirely *inside* the baked container via the caller-supplied
    ``run_in_container`` transport, so it works for both an OVH VPS (``mngr exec``)
    and a slice (direct SSH). Steps:

    1. Bump the container sshd's pre-auth limits (best-effort): the default
       ``MaxStartups=10:30:100`` caps the pre-auth queue tightly and the lease +
       claim flow plus parallel ``mngr observe`` discovery routinely exceeds it.
    2. Wait for the FCT bootstrap's initial-chat sentinel, then destroy the
       bootstrap-created chat agent (named after the bake host) and remove the
       sentinel -- so the user's first lease re-creates the chat agent under their
       own workspace name.

    If no sentinel appears within the timeout the bootstrap never created a chat
    agent (e.g. inference creds absent), so there is nothing to tear down and this
    returns. When the sentinel *is* present the destroy must succeed: a destroy
    error almost always signals a vendored-mngr / FCT-template skew, and shipping a
    pool host whose bootstrap state we don't understand has bitten us before, so we
    raise rather than land a half-known host in the pool.
    """
    sshd_command = shlex.join(["/usr/sbin/sshd", "-o", "MaxSessions=100", "-o", "MaxStartups=100:30:200"])
    sshd_rc, _sshd_out, sshd_err = run_in_container(baked, "sshd-harden", sshd_command, 30.0)
    if sshd_rc != 0:
        logger.warning("Could not harden container sshd for {} (exit {}): {}", host_name, sshd_rc, sshd_err.strip())

    sentinel = shlex.quote(INITIAL_CHAT_SENTINEL_PATH)
    wait_command = (
        f"timeout {int(sentinel_timeout_seconds)} bash -c {shlex.quote(f'until test -f {sentinel}; do sleep 5; done')}"
    )
    wait_rc, _wait_out, wait_err = run_in_container(
        baked, "sentinel-wait", wait_command, float(sentinel_timeout_seconds + 60)
    )
    if wait_rc == _COMMAND_TIMEOUT_EXIT_CODE:
        # The ``timeout`` wrapper killed the wait: the bootstrap never created a
        # chat agent (e.g. inference creds absent), so there is nothing to tear
        # down. This is the only non-zero code we treat as "skip".
        logger.warning(
            "No initial-chat sentinel appeared for {} within {}s; skipping chat-agent teardown",
            host_name,
            sentinel_timeout_seconds,
        )
        return
    if wait_rc != 0:
        # Any other failure (e.g. the container was unreachable -- ssh exit 255)
        # is NOT "no chat agent": silently skipping would ship a pool host with a
        # stale bootstrap chat agent. Fail the bake so the caller can roll back.
        raise PoolBakeError(
            f"waiting for the initial-chat sentinel on {host_name} failed (exit {wait_rc}): {wait_err.strip()}"
        )

    logger.info("  Destroying bootstrap-created chat agent: {}", host_name)
    # Use the canonical in-container mngr invocation (uv run mngr in /mngr/code),
    # which works regardless of transport / login PATH in the FCT image.
    destroy_command = f"cd /mngr/code && uv run mngr destroy {shlex.quote(host_name)} --force"
    destroy_rc, _destroy_out, destroy_err = run_in_container(baked, "chat-destroy", destroy_command, 120.0)
    if destroy_rc != 0:
        raise PoolBakeError(
            f"destroying bootstrap chat agent {host_name!r} failed (exit {destroy_rc}): {destroy_err.strip()}"
        )
    logger.info("  Removing initial-chat sentinel: {}", INITIAL_CHAT_SENTINEL_PATH)
    rm_command = shlex.join(["rm", "-f", INITIAL_CHAT_SENTINEL_PATH])
    rm_rc, _rm_out, rm_err = run_in_container(baked, "sentinel-rm", rm_command, 30.0)
    if rm_rc != 0:
        raise PoolBakeError(
            f"removing initial-chat sentinel {INITIAL_CHAT_SENTINEL_PATH!r} failed (exit {rm_rc}): {rm_err.strip()}"
        )
