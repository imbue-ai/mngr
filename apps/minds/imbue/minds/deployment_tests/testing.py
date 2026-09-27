"""Pytest-facing utilities for the deployment_tests suite.

Separate from ``helpers.py`` because these call into ``pytest`` (skip/fail),
and ``helpers.py`` ships in the built wheel where pytest is not a dependency;
``testing.py`` modules are excluded from the wheel by convention.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from typing import NoReturn

import httpx
import pytest
import tomlkit
from loguru import logger

from imbue.minds.deployment_tests.data_types import DeploymentEnvsConfig
from imbue.minds.deployment_tests.data_types import PoolProvisionInfo
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle

# Opt-out for envs that legitimately have no pool capacity (e.g. an operator's
# dev env without a baked box): restores the old skip-on-503 behavior. The CI
# release flow never sets it -- there, an empty pool means the bake stage broke
# and must FAIL the tests rather than skip them green.
POOL_ALLOW_EMPTY_ENV_VAR: Final[str] = "MINDS_ALLOW_EMPTY_POOL"

# The provider instance the pool tests register in their template clone and
# address their creates at.
POOL_PROVIDER_INSTANCE_NAME: Final[str] = "imbue_cloud_citest"
POOL_SIGNIN_TIMEOUT_SECONDS: Final[int] = 120
# `mngr destroy` and `hosts release` both reach the box and wait on the slice.
POOL_DESTROY_TIMEOUT_SECONDS: Final[int] = 900
POOL_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0


def handle_no_pool_capacity(reason: str) -> NoReturn:
    """React to a no-capacity lease (503): fail by default, skip only on explicit opt-out.

    See specs/remote-workspaces-in-ci.md: once the CI bake stage guarantees pool
    capacity, an empty pool silently skipping these tests green is the worst
    outcome, so the default is a hard failure.
    """
    if os.environ.get(POOL_ALLOW_EMPTY_ENV_VAR) == "1":
        pytest.skip(f"{reason} ({POOL_ALLOW_EMPTY_ENV_VAR}=1 allows envs without pool capacity)")
    pytest.fail(
        f"{reason}. Pool capacity is required by default: the bake stage should have pre-provisioned "
        f"slices (specs/remote-workspaces-in-ci.md). Set {POOL_ALLOW_EMPTY_ENV_VAR}=1 only for envs "
        "that legitimately have no pool."
    )


def require_pool_info(config: DeploymentEnvsConfig) -> PoolProvisionInfo:
    """The run's pre-baked pool info, with the same required-by-default semantics as leasing."""
    if config.pool is None:
        handle_no_pool_capacity("This run has no pre-baked pool (deployment_envs.json carries no pool info)")
    return config.pool


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int,
    env: Mapping[str, str] | None = None,
    logged_command: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, logging the invocation (``logged_command`` redacts secrets in the log line)."""
    logger.info("Running: {}", " ".join(command) if logged_command is None else logged_command)
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        # `env is not None`, not truthiness: an empty mapping means "no environment
        # variables at all", which `env=None` (inherit everything) is not.
        env=dict(env) if env is not None else None,
    )


def prepare_template_clone(
    source_worktree: Path,
    *,
    provider_instance_name: str,
    provider_block: Mapping[str, object],
) -> Path:
    """Clone the orchestrator-prepared template checkout and wire it for imbue_cloud creates.

    The clone gets ``is_allowed_in_pytest = true`` (mngr's config guard refuses
    to run under PYTEST_CURRENT_TEST otherwise) plus ``provider_block``, written
    verbatim as the ``[providers.<provider_instance_name>]`` block the create
    address targets. The caller owns that block because how much of it reaches
    the workspace depends on which create path it drives.
    """
    clone_target = Path(tempfile.mkdtemp(prefix="pool-e2e-dwt-")) / "default-workspace-template"
    # file:// (not a bare path) forces a real transport copy, fully decoupling
    # the clone from the source checkout's object store.
    clone = run_command(["git", "clone", f"file://{source_worktree}", str(clone_target)], timeout=600)
    assert clone.returncode == 0, f"template clone failed: {clone.stderr}"
    settings_path = clone_target / ".mngr" / "settings.toml"
    doc = tomlkit.parse(settings_path.read_text())
    doc["is_allowed_in_pytest"] = True
    providers = doc.setdefault("providers", tomlkit.table())
    instance = tomlkit.table()
    for key, value in provider_block.items():
        instance[key] = value
    providers[provider_instance_name] = instance
    settings_path.write_text(tomlkit.dumps(doc))
    return clone_target


def build_mngr_env(template_path: Path) -> dict[str, str]:
    """The subprocess env for an ``mngr`` call that must read the clone's project config.

    The pytest isolation fixture sets MNGR_ROOT_NAME to a per-test value, which
    makes mngr resolve project config at ``.<root_name>/`` instead of the
    clone's ``.mngr/`` -- point the project config dir at the clone explicitly.
    """
    env = dict(os.environ)
    env["MNGR_PROJECT_CONFIG_DIR"] = str(template_path / ".mngr")
    return env


def parse_created_event(stdout: str) -> tuple[str, str]:
    """The ``(agent_id, host_id)`` from ``mngr create --format jsonl`` output."""
    agent_id, host_id = "", ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "created":
            agent_id = str(event.get("agent_id", ""))
            host_id = str(event.get("host_id", ""))
    assert agent_id and host_id, f"mngr create emitted no created event: {stdout[-2000:]}"
    return agent_id, host_id


def parse_error_event_class(stdout: str) -> str | None:
    """The ``error_class`` of the structured error line ``mngr --format jsonl`` emits, if any.

    The human-formatted message on stderr carries no type, so callers that
    need to branch on which error occurred read it from here -- the same
    ``{"event": "error", "error_class": ...}`` line minds branches on (see
    ``agent_creator``).
    """
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "error":
            error_class = event.get("error_class")
            if isinstance(error_class, str) and error_class:
                return error_class
    return None


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


def list_leased_host_db_ids_or_none(connector_url: str, user: VerifiedUserHandle) -> list[str] | None:
    """The user's leased host db ids via the connector, or None when the listing cannot be read.

    For the teardown path and the release poll: a transient connector error
    there must not mask the test body's real failure (or abort a poll that
    would succeed on the next attempt), so it is logged and reported as None
    instead of raising.
    """
    try:
        with httpx.Client(timeout=POOL_HTTP_TIMEOUT_SECONDS) as client:
            listing = client.get(f"{connector_url}/hosts", headers=_auth_header(user))
    except httpx.HTTPError as exc:
        logger.warning("/hosts listing failed (treated as unreadable): {}", exc)
        return None
    if listing.status_code != 200:
        logger.warning("/hosts listing failed (treated as unreadable): {} {}", listing.status_code, listing.text[:300])
        return None
    return [str(entry["host_db_id"]) for entry in listing.json()]


def list_leased_host_db_ids(connector_url: str, user: VerifiedUserHandle) -> list[str]:
    leased_host_db_ids = list_leased_host_db_ids_or_none(connector_url, user)
    assert leased_host_db_ids is not None, "/hosts listing failed (see the warning above)"
    return leased_host_db_ids


def initialize_and_sign_in(
    template_path: Path,
    mngr_env: Mapping[str, str],
    *,
    account_email: str,
    password: str,
    connector_url: str,
) -> None:
    """Initialize the clone's mngr root and sign ``account_email`` in headlessly.

    ``mngr imbue_cloud auth signin`` requires an already-initialized mngr root
    config (it refuses to create one itself), so a cheap ``mngr list`` runs
    first purely for that side effect. Its exit code is deliberately ignored:
    pre-signin, the imbue_cloud provider's discovery errors by design (no
    session yet), which makes ``mngr list`` exit non-zero even with
    ``--on-error continue``. The signin fails loudly if initialization did not
    happen.

    The session persists in the caller's isolated mngr state and authenticates
    the provider's lease call.
    """
    run_command(
        ["mngr", "list", "--format", "json", "--on-error", "continue"],
        cwd=template_path,
        timeout=POOL_SIGNIN_TIMEOUT_SECONDS,
        env=mngr_env,
    )
    signin = run_command(
        [
            "mngr",
            "imbue_cloud",
            "auth",
            "signin",
            "--account",
            account_email,
            "--password",
            password,
            "--connector-url",
            connector_url,
        ],
        cwd=template_path,
        timeout=POOL_SIGNIN_TIMEOUT_SECONDS,
        env=mngr_env,
        logged_command=f"mngr imbue_cloud auth signin --account {account_email} --password <redacted>",
    )
    assert signin.returncode == 0, f"mngr imbue_cloud auth signin failed: {signin.stderr[-1000:]}"


def tear_down_pool_workspace(
    address: str,
    template_path: Path,
    mngr_env: Mapping[str, str],
    *,
    connector_url: str,
    user: VerifiedUserHandle,
) -> None:
    """Destroy the workspace, release every lease the account still holds, and drop the clone.

    Safe to run unconditionally from a ``finally``: even a create that failed
    partway may have leased a slice. ``mngr destroy`` wipes the workspace but
    deliberately does NOT release the lease -- mngr's GC does that after the
    destroyed-host grace period (see specs/detached-destroy-flow, "No
    imbue_cloud lease release") -- so the explicit release drives the same
    ``hosts release`` path GC would take, eagerly, to return the slice slot
    instead of waiting the grace period out.

    Every failure here is logged rather than raised: raising would mask the
    test body's real failure, and the sweeps reclaim whatever is left behind.
    """
    destroy = run_command(
        ["mngr", "destroy", address, "--force"],
        cwd=template_path,
        timeout=POOL_DESTROY_TIMEOUT_SECONDS,
        env=mngr_env,
    )
    if destroy.returncode != 0:
        logger.warning("Workspace teardown failed (sweeps will reclaim it): {}", destroy.stderr[-500:])
    for leased_host_db_id in list_leased_host_db_ids_or_none(connector_url, user) or []:
        release = run_command(
            ["mngr", "imbue_cloud", "hosts", "release", leased_host_db_id, "--connector-url", connector_url],
            cwd=template_path,
            timeout=POOL_DESTROY_TIMEOUT_SECONDS,
            env=mngr_env,
        )
        if release.returncode != 0:
            logger.warning("Lease release failed (sweeps will reclaim it): {}", release.stderr[-500:])
    shutil.rmtree(template_path.parent, ignore_errors=True)
