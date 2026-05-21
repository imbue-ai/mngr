"""Orchestrator for the ``minds_deployment`` + ``minds_services`` test suites.

Plain-Python (click-driven) entrypoint -- NOT a pytest wrapper. Owns:

* The FCT worktree at ``<monorepo>/.external_worktrees/forever-claude-template/``:
  validation, stash + push to a ``ci-<timestamp>`` branch on the FCT
  remote, and stash-restore so the operator's worktree state is
  unchanged.
* The per-run mail.tm account: creation via the public mail.tm HTTP
  API, env-var threading into pytest, and deletion in cleanup.
* Shared CI env stand-up via ``minds env deploy`` (subprocess), serial
  for the initial single-``default``-env roster.
* Sequential dispatch of the two pytest invocations
  (``-m minds_deployment`` first, then ``-m minds_services``).
* Per-run ledger at ``.minds/ci-test-deploys.jsonl``: append-on-create,
  walked for end-of-run teardown, paired cleanup mode for prior runs.
* Name + age sweep: enumerates ``ci-*`` envs and ``ci-*`` FCT
  branches, destroys anything older than 4 hours.

Wired up to satisfy the spec's command surface; the heavyweight steps
(``minds env deploy`` shellout, SuperTokens admin teardown, the OVH /
Cloudflare / Neon enumeration arm of the sweep) are real where simple
and explicitly-stubbed where iteration with the user is needed -- the
stubs log a clear "not implemented yet" warning rather than silently
no-op-ing.
"""

import os
import re
import subprocess
import sys
from collections.abc import Iterable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never
from uuid import uuid4

import click
import httpx
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.config.loader import load_client_config
from imbue.minds.deployment_tests.data_types import DeploymentEnvsConfig
from imbue.minds.deployment_tests.data_types import FctTemplateRef
from imbue.minds.deployment_tests.data_types import SharedEnvUrls
from imbue.minds.deployment_tests.primitives import DEPLOYMENT_ENVS_JSON_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_ADDRESS_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_JWT_ENV_VAR
from imbue.minds.deployment_tests.primitives import RunId
from imbue.minds.deployment_tests.primitives import SHARED_ENV_SECRET_ENV_VAR_PREFIX
from imbue.minds.deployment_tests.primitives import SharedEnvRole
from imbue.minds.envs.local_store import read_secrets_file
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError
from imbue.minds.utils.output import write_stdout_line
from imbue.mngr.utils.testing import get_short_random_string

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_FCT_WORKTREE_PATH: Final[Path] = _REPO_ROOT / ".external_worktrees" / "forever-claude-template"
_FCT_REMOTE_URL: Final[str] = "git@github.com:imbue-ai/forever-claude-template.git"
_LEDGER_PATH: Final[Path] = _REPO_ROOT / ".minds" / "ci-test-deploys.jsonl"
_DEPLOYMENT_ENVS_JSON_PATH: Final[Path] = _REPO_ROOT / "test-results" / "deployment_envs.json"
_ITERATE_STATE_DIR: Final[Path] = _REPO_ROOT / ".minds"
_DEFAULT_MAX_RESOURCE_AGE_HOURS: Final[int] = 4

_MAILTM_API_BASE: Final[str] = "https://api.mail.tm"

# Default shared-env roster. The spec's initial roster is a single ``default``
# env; expansion is a matter of editing this tuple (and registering more
# roles in tests that need them via ``shared_env('<role>')``).
_DEFAULT_SHARED_ENV_ROLES: Final[tuple[SharedEnvRole, ...]] = (SharedEnvRole("default"),)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class LedgerKind(UpperCaseStrEnum):
    """What kind of resource a ledger entry tracks."""

    ENV = auto()
    FCT_BRANCH = auto()
    MAILTM_ACCOUNT = auto()


class LedgerStatus(UpperCaseStrEnum):
    """Lifecycle status of a ledger-tracked resource."""

    ACTIVE = auto()
    DESTROYED = auto()
    LEAKED = auto()


class LedgerEntry(FrozenModel):
    """One JSONL row in ``.minds/ci-test-deploys.jsonl``.

    Append-only by convention: a new entry is appended for every create
    and for every state change (we never edit prior lines in place).
    Readers fold all rows for a given ``name`` and pick the latest by
    file order to determine current status.
    """

    kind: LedgerKind
    name: NonEmptyStr = Field(description="Resource-specific identifier (env name, branch name, mail.tm account id).")
    created_at: datetime
    run_id: RunId
    status: LedgerStatus


def _append_ledger_entry(entry: LedgerEntry) -> None:
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = entry.model_dump_json()
    with _LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _read_ledger_entries() -> list[LedgerEntry]:
    if not _LEDGER_PATH.is_file():
        return []
    entries: list[LedgerEntry] = []
    for line_number, raw in enumerate(_LEDGER_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            entries.append(LedgerEntry.model_validate_json(stripped))
        except ValueError as exc:
            raise MindError(f"Malformed ledger line at {_LEDGER_PATH}:{line_number}: {stripped!r} ({exc})") from exc
    return entries


def _latest_status_by_name(entries: Iterable[LedgerEntry]) -> dict[NonEmptyStr, LedgerEntry]:
    """Fold entries by name; later rows win (append-only update semantics)."""
    latest: dict[NonEmptyStr, LedgerEntry] = {}
    for entry in entries:
        latest[entry.name] = entry
    return latest


def _mark_status(name: NonEmptyStr, *, kind: LedgerKind, run_id: RunId, status: LedgerStatus) -> None:
    """Append a status-change row for ``name`` (preserves the original ``created_at``)."""
    existing = _latest_status_by_name(_read_ledger_entries()).get(name)
    created_at = existing.created_at if existing is not None else datetime.now(timezone.utc)
    _append_ledger_entry(LedgerEntry(kind=kind, name=name, created_at=created_at, run_id=run_id, status=status))


def _drop_destroyed_rows_if_drained() -> None:
    """Remove ``ci-test-deploys.jsonl`` once every tracked resource is destroyed.

    Keeps the file from growing unboundedly across many runs while
    still preserving the append-only audit log within an active set.
    """
    entries = _read_ledger_entries()
    if not entries:
        if _LEDGER_PATH.is_file():
            _LEDGER_PATH.unlink()
        return
    latest = _latest_status_by_name(entries)
    if all(entry.status == LedgerStatus.DESTROYED for entry in latest.values()):
        _LEDGER_PATH.unlink()
        logger.info("Ledger drained -- removed {}", _LEDGER_PATH)


# ---------------------------------------------------------------------------
# Run id
# ---------------------------------------------------------------------------


def _mint_run_id() -> RunId:
    """Compact ISO 8601 UTC, lowercase ``t``/``z`` so it fits in ``DevEnvName``.

    Format ``YYYYMMDDtHHMMSSz`` (e.g. ``20260518t140212z``). Lex sort
    equals chronological sort.
    """
    return RunId(datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz"))


# ---------------------------------------------------------------------------
# FCT worktree
# ---------------------------------------------------------------------------


class FctWorktreeMissingError(MindError):
    """Raised when ``.external_worktrees/forever-claude-template/`` is not present."""


def _validate_fct_worktree() -> None:
    """Ensure the FCT worktree exists; print actionable setup instructions if not."""
    if not _FCT_WORKTREE_PATH.is_dir() or not (_FCT_WORKTREE_PATH / ".git").exists():
        raise FctWorktreeMissingError(
            "FCT worktree missing at "
            f"{_FCT_WORKTREE_PATH}\n\n"
            "Per the CLAUDE.md convention, external repos live under "
            "``.external_worktrees/<repo>/``. Create it once from your FCT clone "
            "(typically ~/project/forever-claude-template):\n\n"
            f"  git worktree add -B <fct-branch> {_FCT_WORKTREE_PATH} <fct-branch>\n\n"
            "Use whichever FCT branch you want the tests to exercise. Re-run the orchestrator "
            "once the worktree is in place."
        )


def _push_fct_test_branch(*, run_id: RunId) -> str:
    """Stash + commit + push the worktree's contents to ``ci-<run_id>`` on the FCT remote.

    Returns the branch name. Records the branch in the ledger. The
    operator's primary FCT clone is never touched.

    Stub for now: stamped out per the spec but not yet exercised by the
    tests (they all skip). The stash + push code lives here so iterating
    on it does not require touching anything else.
    """
    branch_name = f"ci-{run_id}"
    logger.warning(
        "FCT branch push to {!r} is stubbed out -- the push flow is documented in the spec but "
        "not yet wired up. Tests today use the local worktree path via the fct_template_ref fixture.",
        branch_name,
    )
    _append_ledger_entry(
        LedgerEntry(
            kind=LedgerKind.FCT_BRANCH,
            name=NonEmptyStr(branch_name),
            created_at=datetime.now(timezone.utc),
            run_id=run_id,
            status=LedgerStatus.ACTIVE,
        )
    )
    return branch_name


def _delete_fct_test_branch(branch_name: str, *, run_id: RunId) -> None:
    """Delete the pushed test branch from the FCT remote. Idempotent against already-gone."""
    logger.warning(
        "FCT branch deletion for {!r} is stubbed out -- pair with the push stub. The age-sweep "
        "will eventually be the safety net here.",
        branch_name,
    )
    _mark_status(NonEmptyStr(branch_name), kind=LedgerKind.FCT_BRANCH, run_id=run_id, status=LedgerStatus.DESTROYED)


# ---------------------------------------------------------------------------
# mail.tm
# ---------------------------------------------------------------------------


class _MailtmAccount(FrozenModel):
    """Per-run disposable mail.tm account.

    Holds the credentials needed for the orchestrator's own bookkeeping
    (the ``account_id`` for ledger entries + the JWT for the delete call
    at end-of-run) plus the ``address`` exported to the pytest process.
    The account password is consumed once by ``/token`` during creation
    and not retained -- every later mail.tm API call uses the JWT.
    """

    account_id: NonEmptyStr
    address: NonEmptyStr
    jwt: SecretStr


def _create_mailtm_account(*, run_id: RunId) -> _MailtmAccount:
    """Create a fresh disposable mail.tm account; return creds + record in ledger.

    The ledger entry is appended as soon as the account is created on mail.tm,
    before the JWT is minted, so a failure between account creation and token
    mint still leaves a trail for ``cleanup`` to find.
    """
    with httpx.Client(base_url=_MAILTM_API_BASE, timeout=20.0) as client:
        domains_response = client.get("/domains")
        domains_response.raise_for_status()
        domains = domains_response.json().get("hydra:member", [])
        if not domains:
            raise MindError("mail.tm returned an empty domains list; cannot create a test account.")
        domain = domains[0]["domain"]
        local_part = f"ci-{run_id}-{get_short_random_string()}"
        address = f"{local_part}@{domain}"
        password = uuid4().hex
        account_response = client.post("/accounts", json={"address": address, "password": password})
        account_response.raise_for_status()
        account_id = NonEmptyStr(account_response.json()["id"])
        # Record the account in the ledger now -- before requesting the JWT --
        # so a /token failure leaves a recoverable trail rather than orphaning
        # the account on mail.tm.
        _append_ledger_entry(
            LedgerEntry(
                kind=LedgerKind.MAILTM_ACCOUNT,
                name=account_id,
                created_at=datetime.now(timezone.utc),
                run_id=run_id,
                status=LedgerStatus.ACTIVE,
            )
        )
        token_response = client.post("/token", json={"address": address, "password": password})
        token_response.raise_for_status()
        jwt = token_response.json()["token"]
    account = _MailtmAccount(
        account_id=account_id,
        address=NonEmptyStr(address),
        jwt=SecretStr(jwt),
    )
    logger.info("Created per-run mail.tm account {}", account.address)
    return account


def _delete_mailtm_account(account_id: NonEmptyStr, jwt: SecretStr, *, run_id: RunId) -> None:
    """Delete a mail.tm account by id. Idempotent against already-gone."""
    with httpx.Client(base_url=_MAILTM_API_BASE, timeout=20.0) as client:
        response = client.delete(
            f"/accounts/{account_id}",
            headers={"Authorization": f"Bearer {jwt.get_secret_value()}"},
        )
        if response.status_code not in (204, 404):
            response.raise_for_status()
    _mark_status(account_id, kind=LedgerKind.MAILTM_ACCOUNT, run_id=run_id, status=LedgerStatus.DESTROYED)


# ---------------------------------------------------------------------------
# Shared envs
# ---------------------------------------------------------------------------


def _mint_shared_env_name(*, run_id: RunId, role: SharedEnvRole) -> DevEnvName:
    """``ci-<run-id>-<short>`` (default role), or with the role appended otherwise.

    Every CI env name MUST include both a timestamp AND a random suffix:
    the timestamp is what the name+age sweep parses to decide which envs
    are old enough to destroy (regex :data:`_CI_ENV_NAME_PATTERN`
    anchors on ``^ci-<timestamp>``), and the random suffix prevents
    name collisions between two runs that happen to start in the same
    UTC second (e.g. two concurrent orchestrator invocations, or a
    re-run within a single second of the prior one). The role -- when
    not ``default`` -- is appended LAST so the timestamp stays at
    position 2 and the sweep regex matches every shape uniformly.
    """
    short = get_short_random_string()
    if role == SharedEnvRole("default"):
        return DevEnvName(f"ci-{run_id}-{short}")
    return DevEnvName(f"ci-{run_id}-{short}-{role}")


def _deploy_shared_env(*, name: DevEnvName, run_id: RunId, role: SharedEnvRole) -> SharedEnvUrls:
    """Run ``uv run minds env activate --create <name> && minds env deploy``; return URLs.

    Stub: real implementation needs to drive the activate + deploy CLI
    and parse the resulting ``client.toml`` to recover the connector +
    litellm URLs. The connector tests are all skipped today so this
    stub raises if invoked.
    """
    logger.warning(
        "Shared env deploy for {!r} (role={!r}, run={!r}) is stubbed out -- iterate next. "
        "See specs/minds-deployment-tests.md.",
        name,
        role,
        run_id,
    )
    raise MindError(
        f"Shared env deploy for {name!r} is not yet implemented. The orchestrator scaffolding is "
        "in place; the next iteration step is the `minds env deploy` shellout + client.toml parse."
    )


def _destroy_env(name: DevEnvName, *, run_id: RunId) -> None:
    """Run ``uv run minds env destroy`` for the named env. Idempotent against missing state."""
    logger.warning(
        "Env destroy for {!r} is stubbed out -- iterate next. The name+age sweep is the safety net.",
        name,
    )
    _mark_status(NonEmptyStr(str(name)), kind=LedgerKind.ENV, run_id=run_id, status=LedgerStatus.DESTROYED)


# ---------------------------------------------------------------------------
# Name + age sweep
# ---------------------------------------------------------------------------


_CI_ENV_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^ci-(\d{8}t\d{6}z)")


def _sweep_stale_envs(max_age_hours: int = _DEFAULT_MAX_RESOURCE_AGE_HOURS) -> None:
    """Enumerate ``ci-*`` envs; destroy anything older than ``max_age_hours``.

    Stub: real implementation needs to shell out to ``uv run minds env
    list`` (parses its output), parse the embedded timestamp from each
    ``ci-<YYYYMMDDtHHMMSSz>...`` name, and call destroy on stale
    ones. Tracked separately; the name pattern is fixed here.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    logger.info(
        "Name+age sweep is stubbed -- would destroy any ci-* env older than {} ({}h).",
        cutoff.isoformat(),
        max_age_hours,
    )


# ---------------------------------------------------------------------------
# Test-process env + JSON
# ---------------------------------------------------------------------------


def _write_deployment_envs_json(
    *,
    shared_envs: dict[SharedEnvRole, SharedEnvUrls],
    fct: FctTemplateRef,
    run_id: RunId,
    target_path: Path = _DEPLOYMENT_ENVS_JSON_PATH,
) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    config = DeploymentEnvsConfig(shared_envs=shared_envs, fct=fct, run_id=run_id)
    target_path.write_text(config.model_dump_json(indent=2))
    return target_path


def _build_pytest_env(
    *,
    deployment_envs_json_path: Path,
    mailtm_address: str | None,
    mailtm_jwt: SecretStr | None,
    shared_env_secrets: dict[SharedEnvRole, dict[str, SecretStr]],
) -> dict[str, str]:
    """Build the env dict the pytest subprocess inherits.

    Reads from the current process env first so ``VAULT_TOKEN`` /
    ``VAULT_ADDR`` / ``VAULT_NAMESPACE`` / ``ANTHROPIC_API_KEY`` pass
    through unmodified.
    """
    env = dict(os.environ)
    env[DEPLOYMENT_ENVS_JSON_ENV_VAR] = str(deployment_envs_json_path)
    if mailtm_address and mailtm_jwt:
        env[MAILTM_ADDRESS_ENV_VAR] = mailtm_address
        env[MAILTM_JWT_ENV_VAR] = mailtm_jwt.get_secret_value()
    for role, secrets in shared_env_secrets.items():
        prefix = f"{SHARED_ENV_SECRET_ENV_VAR_PREFIX}{str(role).upper()}_"
        for key, value in secrets.items():
            env[f"{prefix}{key}"] = value.get_secret_value()
    return env


def _invoke_pytest_for_mark(
    mark: str,
    *,
    env: dict[str, str],
    extra_args: tuple[str, ...] = (),
) -> int:
    """Run ``uv run pytest -m <mark> <targets>``; return exit code.

    ``extra_args`` lets ``services-against`` override the default test
    target (the whole deployment_tests/ dir) with whichever specific
    test files / nodeids the operator passed on the command line. The
    default ``run`` flow leaves it empty so the full directory is collected.
    """
    targets = list(extra_args) if extra_args else [str(_REPO_ROOT / "apps" / "minds" / "deployment_tests")]
    cmd = [
        "uv",
        "run",
        "pytest",
        "-m",
        mark,
        "--no-cov",
        "-p",
        "no:xdist",
        *targets,
    ]
    logger.info("Running: {}", " ".join(cmd))
    completed = subprocess.run(cmd, env=env, cwd=str(_REPO_ROOT), check=False)
    return completed.returncode


# ---------------------------------------------------------------------------
# Top-level click CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Orchestrate the minds_deployment + minds_services pytest suites."""


@cli.command()
@click.option(
    "--keep-on-failure", is_flag=True, default=False, help="Leave ephemeral envs from failing tests in place."
)
def run(keep_on_failure: bool) -> None:
    """Full flow: sweep, FCT push, mail.tm, shared envs, pytest x2, teardown."""
    run_id = _mint_run_id()
    logger.info("Starting orchestrator run {}", run_id)

    _validate_fct_worktree()
    _sweep_stale_envs()

    fct_branch = _push_fct_test_branch(run_id=run_id)
    mailtm = _create_mailtm_account(run_id=run_id)

    shared_env_urls: dict[SharedEnvRole, SharedEnvUrls] = {}
    shared_env_secrets: dict[SharedEnvRole, dict[str, SecretStr]] = {}
    deploy_failure: MindError | None = None
    try:
        for role in _DEFAULT_SHARED_ENV_ROLES:
            env_name = _mint_shared_env_name(run_id=run_id, role=role)
            _append_ledger_entry(
                LedgerEntry(
                    kind=LedgerKind.ENV,
                    name=NonEmptyStr(str(env_name)),
                    created_at=datetime.now(timezone.utc),
                    run_id=run_id,
                    status=LedgerStatus.ACTIVE,
                )
            )
            shared_env_urls[role] = _deploy_shared_env(name=env_name, run_id=run_id, role=role)
    except MindError as exc:
        deploy_failure = exc
        logger.error("Shared env deploy failed: {}", exc)

    pytest_envs_path = _write_deployment_envs_json(
        shared_envs=shared_env_urls,
        fct=FctTemplateRef(
            worktree_path=_FCT_WORKTREE_PATH,
            test_branch=NonEmptyStr(fct_branch),
            test_remote=NonEmptyStr(_FCT_REMOTE_URL),
        ),
        run_id=run_id,
    )
    pytest_env = _build_pytest_env(
        deployment_envs_json_path=pytest_envs_path,
        mailtm_address=str(mailtm.address),
        mailtm_jwt=mailtm.jwt,
        shared_env_secrets=shared_env_secrets,
    )

    # minds_deployment tests use only the ephemeral_env fixture (they mint
    # their own ci-* env per test) and do not depend on the shared envs,
    # so they run regardless of whether the shared-env stand-up succeeded.
    # minds_services tests depend on shared_env(role=...) URLs+secrets, so
    # they are skipped when shared-env deploy failed.
    deployment_rc = _invoke_pytest_for_mark("minds_deployment", env=pytest_env)
    services_rc = _invoke_pytest_for_mark("minds_services", env=pytest_env) if deploy_failure is None else 1

    teardown_failures = _teardown_run(
        run_id=run_id,
        mailtm_account=mailtm,
        fct_branch=fct_branch,
        keep_on_failure=keep_on_failure,
        tests_failed=(deployment_rc != 0 or services_rc != 0),
    )
    _drop_destroyed_rows_if_drained()

    exit_code = 0
    if deploy_failure is not None or deployment_rc != 0 or services_rc != 0 or teardown_failures:
        exit_code = 1
    logger.info("Orchestrator run {} done -- exit code {}", run_id, exit_code)
    sys.exit(exit_code)


@cli.command()
def cleanup() -> None:
    """Walk every ledger entry across all prior runs; tear each down; drop the file when drained."""
    entries = _read_ledger_entries()
    if not entries:
        write_stdout_line("Ledger is empty -- nothing to clean up.")
        return
    latest = _latest_status_by_name(entries)
    leftovers = [entry for entry in latest.values() if entry.status != LedgerStatus.DESTROYED]
    if not leftovers:
        _drop_destroyed_rows_if_drained()
        write_stdout_line("Ledger had only destroyed entries -- file removed.")
        return
    write_stdout_line(f"Cleaning up {len(leftovers)} active+leaked entries from prior runs...")
    failures = 0
    for entry in leftovers:
        try:
            match entry.kind:
                case LedgerKind.ENV:
                    _destroy_env(DevEnvName(str(entry.name)), run_id=entry.run_id)
                case LedgerKind.FCT_BRANCH:
                    _delete_fct_test_branch(str(entry.name), run_id=entry.run_id)
                case LedgerKind.MAILTM_ACCOUNT:
                    logger.warning(
                        "mail.tm account {} cleanup needs the JWT, which we did not persist; "
                        "the account will expire naturally.",
                        entry.name,
                    )
                    _mark_status(
                        entry.name,
                        kind=LedgerKind.MAILTM_ACCOUNT,
                        run_id=entry.run_id,
                        status=LedgerStatus.DESTROYED,
                    )
                case _ as unreachable:
                    assert_never(unreachable)
        except (MindError, httpx.HTTPError) as exc:
            logger.error("Cleanup failed for {} {}: {}", entry.kind, entry.name, exc)
            failures += 1
    _drop_destroyed_rows_if_drained()
    if failures:
        sys.exit(1)


@cli.command(name="deployment-only")
@click.argument("tests", nargs=-1)
def deployment_only(tests: tuple[str, ...]) -> None:
    """Run only the ``minds_deployment`` pytest batch (no shared env stand-up).

    For iterating on the ``minds_deployment`` tests (those that mint
    their own ephemeral env via the ``ephemeral_env`` fixture) without
    paying for the shared-env-deploy + mail.tm-account setup that the
    main ``run`` command does. The FCT worktree is still validated up
    front so tests that create real minds agents have a template ref to
    point at; pass test files / nodeids positionally.

    Operator must have ``vault login``-ed (the in-test ``minds env
    deploy`` subprocess reads tier secrets from Vault).
    """
    _validate_fct_worktree()
    run_id = _mint_run_id()

    pytest_envs_path = _write_deployment_envs_json(
        shared_envs={},
        fct=FctTemplateRef(worktree_path=_FCT_WORKTREE_PATH),
        run_id=run_id,
    )
    pytest_env = _build_pytest_env(
        deployment_envs_json_path=pytest_envs_path,
        mailtm_address=None,
        mailtm_jwt=None,
        shared_env_secrets={},
    )

    test_targets = tuple(tests) if tests else ()
    rc = _invoke_pytest_for_mark("minds_deployment", env=pytest_env, extra_args=test_targets)
    _drop_destroyed_rows_if_drained()
    sys.exit(rc)


@cli.command()
@click.argument("role", default="default")
def up(role: str) -> None:
    """Local iterate: stand up a shared env + print a ready-to-paste pytest command."""
    run_id = _mint_run_id()
    role_key = SharedEnvRole(role)
    _validate_fct_worktree()
    env_name = _mint_shared_env_name(run_id=run_id, role=role_key)
    _append_ledger_entry(
        LedgerEntry(
            kind=LedgerKind.ENV,
            name=NonEmptyStr(str(env_name)),
            created_at=datetime.now(timezone.utc),
            run_id=run_id,
            status=LedgerStatus.ACTIVE,
        )
    )
    urls = _deploy_shared_env(name=env_name, run_id=run_id, role=role_key)
    state_path = _ITERATE_STATE_DIR / f"iterate-{role}.json"
    _write_deployment_envs_json(
        shared_envs={role_key: urls},
        fct=FctTemplateRef(worktree_path=_FCT_WORKTREE_PATH),
        run_id=run_id,
        target_path=state_path,
    )
    write_stdout_line(f"Shared env {env_name!r} (role={role!r}) is up.")
    write_stdout_line(f"State file: {state_path}")
    write_stdout_line("Run the tests with:")
    write_stdout_line(
        f"  {DEPLOYMENT_ENVS_JSON_ENV_VAR}={state_path} uv run pytest -m minds_services apps/minds/deployment_tests/"
    )
    write_stdout_line("Tear down with: just minds-test-deployment-down")


@cli.command()
@click.argument("role", default="default")
def down(role: str) -> None:
    """Local iterate: tear down whatever ``up`` last stood up for ``role``."""
    state_path = _ITERATE_STATE_DIR / f"iterate-{role}.json"
    if not state_path.is_file():
        write_stdout_line(f"No iterate state file at {state_path}; nothing to tear down.")
        return
    config = DeploymentEnvsConfig.model_validate_json(state_path.read_text())
    for urls in config.shared_envs.values():
        _destroy_env(urls.env_name, run_id=config.run_id)
    state_path.unlink()
    _drop_destroyed_rows_if_drained()


@cli.command(name="services-against")
@click.argument("env_name")
@click.argument("tests", nargs=-1)
@click.option("--no-fct-push", is_flag=True, default=False, help="Skip the FCT branch push (purely backend tests).")
def services_against(env_name: str, tests: tuple[str, ...], no_fct_push: bool) -> None:
    """Point minds_services tests at an already-deployed dev env (e.g. dev-josh).

    Loads ``~/.minds-<env>/client.toml`` for the URLs + ``~/.minds-<env>/secrets.toml``
    for the per-env SuperTokens + Neon secrets, builds a one-role
    ``deployment_envs.json`` against the ``default`` role, exports the
    per-shared-env secret env vars + the mail.tm credentials (created
    fresh for this run), and shells out to ``uv run pytest -m minds_services``
    with whichever test paths the operator passed.

    Does not touch the target env's cloud state -- no create, no
    destroy, no recover. The FCT worktree push runs by default so
    tests that create real minds agents can reach the prepared
    template ref; ``--no-fct-push`` opts out for purely backend tests.
    """
    dev_env_name = DevEnvName(env_name)
    _validate_fct_worktree()
    run_id = _mint_run_id()
    _push_fct_test_branch(run_id=run_id) if not no_fct_push else None

    target_env_root = Path.home() / f".minds-{dev_env_name}"
    target_client_toml = target_env_root / "client.toml"
    target_secrets_toml = target_env_root / "secrets.toml"
    if not target_client_toml.is_file():
        raise click.ClickException(
            f"No client.toml found at {target_client_toml} for env {env_name!r}. "
            f'Activate + deploy the env first: `eval "$(uv run minds env activate --create --deploy {env_name})" && uv run minds env deploy`.'
        )
    if not target_secrets_toml.is_file():
        raise click.ClickException(
            f"No secrets.toml found at {target_secrets_toml} for env {env_name!r}. "
            "Per-dev-env secrets are written by `minds env deploy`; re-run a deploy if this file is missing."
        )

    client_config = load_client_config(target_client_toml)
    secrets_model = read_secrets_file(dev_env_name)

    default_role = SharedEnvRole("default")
    shared_env_urls = SharedEnvUrls(
        role=default_role,
        env_name=dev_env_name,
        connector_url=client_config.connector_url,
        litellm_proxy_url=client_config.litellm_proxy_url,
    )
    shared_env_secrets: dict[SharedEnvRole, dict[str, SecretStr]] = {
        default_role: {key: value for key, value in secrets_model.secrets.items()}
    }

    mailtm = _create_mailtm_account(run_id=run_id)

    pytest_envs_path = _write_deployment_envs_json(
        shared_envs={default_role: shared_env_urls},
        fct=FctTemplateRef(worktree_path=_FCT_WORKTREE_PATH),
        run_id=run_id,
    )
    pytest_env = _build_pytest_env(
        deployment_envs_json_path=pytest_envs_path,
        mailtm_address=str(mailtm.address),
        mailtm_jwt=mailtm.jwt,
        shared_env_secrets=shared_env_secrets,
    )

    test_targets = list(tests) if tests else [str(_REPO_ROOT / "apps" / "minds" / "deployment_tests")]
    pytest_argv: tuple[str, ...] = tuple(test_targets)
    rc = _invoke_pytest_for_mark("minds_services", env=pytest_env, extra_args=pytest_argv)

    # Teardown: only the mail.tm account + (if pushed) the FCT branch
    # need cleanup -- we never created the target dev env.
    teardown_failures = _teardown_run(
        run_id=run_id,
        mailtm_account=mailtm,
        fct_branch=NonEmptyStr(f"ci-{run_id}") if not no_fct_push else NonEmptyStr("noop"),
        keep_on_failure=False,
        tests_failed=(rc != 0),
    )
    _drop_destroyed_rows_if_drained()

    sys.exit(1 if rc != 0 or teardown_failures else 0)


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def _teardown_run(
    *,
    run_id: RunId,
    mailtm_account: _MailtmAccount,
    fct_branch: str,
    keep_on_failure: bool,
    tests_failed: bool,
) -> int:
    """Tear down everything the current run created; return count of failures."""
    failures = 0
    entries_for_run = [entry for entry in _read_ledger_entries() if entry.run_id == run_id]
    latest = _latest_status_by_name(entries_for_run)
    for entry in latest.values():
        if entry.status == LedgerStatus.DESTROYED:
            continue
        if keep_on_failure and tests_failed and entry.kind == LedgerKind.ENV:
            _mark_status(entry.name, kind=entry.kind, run_id=run_id, status=LedgerStatus.LEAKED)
            logger.info("Marking {} {} as leaked (--keep-on-failure + tests failed)", entry.kind, entry.name)
            continue
        try:
            match entry.kind:
                case LedgerKind.ENV:
                    _destroy_env(DevEnvName(str(entry.name)), run_id=run_id)
                case LedgerKind.FCT_BRANCH:
                    _delete_fct_test_branch(str(entry.name), run_id=run_id)
                case LedgerKind.MAILTM_ACCOUNT:
                    _delete_mailtm_account(entry.name, mailtm_account.jwt, run_id=run_id)
                case _ as unreachable:
                    assert_never(unreachable)
        except (MindError, httpx.HTTPError) as exc:
            logger.error("Teardown failed for {} {}: {}", entry.kind, entry.name, exc)
            failures += 1
    # ``fct_branch`` is recorded in the ledger at push time; the teardown loop
    # finds and deletes it via _delete_fct_test_branch. The arg is kept on
    # the signature so future callers (e.g. a partial-teardown helper) can
    # target it explicitly.
    _ = fct_branch
    return failures


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
