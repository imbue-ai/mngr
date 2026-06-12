"""Per-dev-env Modal Secret + Modal app deploy helpers.

`minds env deploy <name>` builds on these to push the per-env Modal
Secrets that ``litellm-proxy`` and ``remote_service_connector`` consume,
then deploys both Modal apps to the per-env Modal environment.

Each Modal Secret pushed here is *tier-shared values from Vault* layered
with *per-env overrides* computed at deploy time:

* ``AUTH_WEBSITE_DOMAIN`` -- the per-env connector URL
* ``SUPERTOKENS_CONNECTION_URI`` -- the tier core URL plus an
  ``/appid-<name>`` suffix (multi-tenant per-app routing)
* ``DATABASE_URL`` (under ``neon``) -- the per-env Neon DB DSN
* ``LITELLM_PROXY_URL`` (under ``litellm-connector``) -- the per-env
  LiteLLM proxy URL

Modal Secrets are env-scoped; pushes target the per-dev Modal env so
they line up with the deploys.

Vault entries that don't exist yet (e.g. Cloudflare before the operator
sets it up) get a single-key placeholder Modal Secret so ``modal
deploy`` doesn't fail with "Secret not found". Routes inside the
connector that consume those values will 500 at request time until the
Vault entry gets populated and ``minds env deploy`` is re-run.
"""

import json
import os
import re
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import AnyUrl

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import info_span
from imbue.minds.envs.primitives import DeployStrategy
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.providers.neon_db import NeonProjectRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.errors import MindError

# Modal's `modal deploy` prints lines like:
#     Created web function api => https://<host>.modal.run
# Under the shortened app + function names (``rsc-<tier>``/``api`` and
# ``llm-<tier>``/``proxy``) the natural host always fits under DNS's
# 63-char limit, so no truncation / 6-hex-suffix surfaces in practice.
# We still collapse whitespace before regex matching in case Modal
# wraps the URL across stdout lines for terminal display reasons.
_MODAL_DEPLOY_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"https://[A-Za-z0-9_\-.]+\.modal\.run")

# Services that need a per-env Modal Secret backed by a Vault entry.
# Each name corresponds to an entry under ``.minds/template/<name>.sh``
# and produces a Modal Secret named ``<name>-<tier>-<deploy_id>`` via
# ``build_per_env_secret_values``. The ``litellm-connector`` Modal
# Secret is NOT in this list -- it's a code-driven secret (no Vault
# entry exists or is expected) pushed by ``provisioning.deploy_env``
# directly; see the corresponding step in that function.
_PER_ENV_SECRET_SERVICES: Final[tuple[str, ...]] = (
    "litellm",
    "supertokens",
    "cloudflare",
    "neon",
    "pool-ssh",
    # OVH AK/AS/CK -- the connector's release route + cleanup cron make signed
    # OVH calls at runtime to strip per-lease tags and cancel released VPSes.
    "ovh",
)

# Placeholder key written when a Vault entry isn't populated yet. Modal
# requires at least one KEY=VALUE pair to create a Secret; this gives us
# one without exposing anything meaningful, so ``modal deploy`` can still
# reference the Secret.
_PLACEHOLDER_KEY: Final[str] = "MNGR_PLACEHOLDER"
_PLACEHOLDER_VALUE: Final[str] = "unpopulated"

_MODAL_DEPLOY_TIMEOUT_SECONDS: Final[float] = 600.0
_MODAL_SECRET_TIMEOUT_SECONDS: Final[float] = 60.0
_MODAL_ENV_CREATE_TIMEOUT_SECONDS: Final[float] = 60.0

# Env-var names the deployed modal apps read at module load to pin
# their warm-pool size. Kept here (one name per app) so the deploy
# side and the app side stay in lockstep -- changing either name in
# isolation would silently fall back to the in-app default (0).
CONNECTOR_MIN_CONTAINERS_ENV_VAR: Final[str] = "MINDS_CONNECTOR_MIN_CONTAINERS"
LITELLM_PROXY_MIN_CONTAINERS_ENV_VAR: Final[str] = "MINDS_LITELLM_PROXY_MIN_CONTAINERS"

# Env-var names the deployed modal apps read at module load to set their
# idle-before-scaledown window (seconds). Same lockstep contract as the
# min-containers names above. ``0`` (the in-app default) means "use Modal's
# own default scaledown window".
CONNECTOR_SCALEDOWN_WINDOW_ENV_VAR: Final[str] = "MINDS_CONNECTOR_SCALEDOWN_WINDOW"
LITELLM_PROXY_SCALEDOWN_WINDOW_ENV_VAR: Final[str] = "MINDS_LITELLM_PROXY_SCALEDOWN_WINDOW"

# Env-var name the deployed modal apps read at module load to pick
# which timestamped Modal Secret bundle to attach. Mirrors the same
# constant in ``secret_lifecycle.py``; kept here (instead of importing
# from there) because ``per_env_deploy`` predates that module and we
# avoid the circular import.
MINDS_DEPLOY_ID_ENV_VAR: Final[str] = "MINDS_DEPLOY_ID"

# Map our ``DeployStrategy`` enum (whose member names match the
# operator-facing ``--hard`` / ``--soft`` vocabulary) to the literal
# argument values ``modal deploy --strategy`` expects on the CLI.
# Modal calls the no-downtime variant ``rolling``; our matching enum
# member is :attr:`DeployStrategy.ROLLOVER` (chosen so the operator
# language "we deployed with the rollover strategy" reads naturally
# alongside ``--soft``).
_MODAL_STRATEGY_ARG_BY_ENUM: Final[dict[DeployStrategy, str]] = {
    DeployStrategy.ROLLOVER: "rolling",
    DeployStrategy.RECREATE: "recreate",
}


class ModalDeployError(MindError):
    """Raised when a ``modal deploy`` or ``modal secret create`` call fails."""


class RepoLayoutError(MindError):
    """Raised when the Modal app files can't be located on disk.

    ``minds env deploy`` shells out to ``modal deploy`` with an absolute
    path to the Modal app file. The path is computed relative to this
    module's location, which assumes the operator is running from the
    monorepo (the only place ``minds env deploy`` is expected to run
    from today). When the layout isn't right we surface a clear error
    instead of letting ``modal deploy`` fail with a confusing one.
    """


def _repo_root() -> Path:
    """Return the monorepo root by walking up from this module's location.

    Layout: ``<repo>/apps/minds/imbue/minds/envs/per_env_deploy.py``
    -- five ``parents`` hops gets us to ``<repo>``.
    """
    root = Path(__file__).resolve().parents[5]
    if not (root / "apps").is_dir():
        raise RepoLayoutError(
            f"Expected repo root at {root} but no `apps/` directory there. "
            "`minds env deploy` must be run from a checkout of the monorepo."
        )
    return root


def _litellm_app_file() -> Path:
    return _repo_root() / "apps" / "modal_litellm" / "app.py"


def _connector_app_file() -> Path:
    return _repo_root() / "apps" / "remote_service_connector" / "imbue" / "remote_service_connector" / "app.py"


def per_env_connector_url(name: DevEnvName, modal_workspace: str, *, tier: str) -> AnyUrl:
    """Compute the connector's URL for the given dev env.

    Modal asgi apps follow ``<workspace>--<app>-<function>.modal.run``,
    with the env name embedded as ``<workspace>-<env>--<app>-...``
    (Modal's URL convention for non-default envs). The connector's app
    name is ``rsc-<tier>`` and its FastAPI function is ``api`` -- short
    enough that the full hostname always fits under DNS's 63-char
    limit, so the computed URL is exactly what Modal returns (no
    truncation, no fixup pass).

    Today only the dev tier uses ``modal_env_strategy=PER_ENV``, so
    ``tier`` is effectively always ``"dev"`` in practice; the parameter
    is here so a future PER_ENV tier (a hypothetical ``staging-dev`` or
    similar) gets the right ``rsc-<tier>`` segment without further
    changes.
    """
    return AnyUrl(f"https://{modal_workspace}-{name}--rsc-{tier}-api.modal.run")


def per_env_litellm_proxy_url(name: DevEnvName, modal_workspace: str, *, tier: str) -> AnyUrl:
    """Compute the LiteLLM proxy's URL for the given dev env.

    Same hostname convention as :func:`per_env_connector_url`; the
    proxy's app name is ``llm-<tier>`` and its asgi function is ``proxy``.
    """
    return AnyUrl(f"https://{modal_workspace}-{name}--llm-{tier}-proxy.modal.run")


def tier_connector_url(tier: str, modal_workspace: str) -> AnyUrl:
    """Compute the connector's URL for a shared-tier deploy (staging / production).

    Shared tiers deploy into Modal's default-named environment (no env
    name in the URL), so the host shape is
    ``<workspace>--<app>-<function>.modal.run`` -- one fewer segment
    than the per-env shape.
    """
    return AnyUrl(f"https://{modal_workspace}--rsc-{tier}-api.modal.run")


def tier_litellm_proxy_url(tier: str, modal_workspace: str) -> AnyUrl:
    """Compute the LiteLLM proxy's URL for a shared-tier deploy."""
    return AnyUrl(f"https://{modal_workspace}--llm-{tier}-proxy.modal.run")


def build_per_env_secret_values(
    service: str,
    *,
    tier_vault_prefix: str,
    overrides: dict[str, str],
    parent_cg: ConcurrencyGroup,
) -> dict[str, str]:
    """Read tier-shared values for one service from Vault and layer overrides.

    Missing Vault entries return an empty dict and emit a warning so
    the operator can populate them later; the caller is expected to
    fall back to a placeholder when both the tier-shared values and
    overrides come up empty.

    This helper is only for genuinely Vault-backed services (every
    entry in ``_PER_ENV_SECRET_SERVICES``). Code-driven secrets like
    ``litellm-connector`` go through ``provisioning.deploy_env``'s
    direct ``push_per_env_modal_secret`` call instead -- they have no
    Vault entry to read.
    """
    base: dict[str, str] = {}
    try:
        base = read_vault_kv(
            VaultPath(f"{tier_vault_prefix}/{service}"),
            parent_concurrency_group=parent_cg,
        )
    except VaultReadError as exc:
        logger.warning("Vault read for {} failed ({}); will push placeholder.", service, exc)
    merged = dict(base)
    merged.update(overrides)
    return {k: v for k, v in merged.items() if v}


def push_per_env_modal_secret(
    secret_name: str,
    values: dict[str, str],
    *,
    modal_env: str,
    parent_cg: ConcurrencyGroup,
) -> None:
    """Upsert a Modal Secret in env ``modal_env``.

    Empty ``values`` is replaced with a single placeholder so
    ``modal secret create`` (which requires at least one KEY=VALUE pair)
    succeeds and downstream ``modal deploy`` can still reference the
    Secret. The placeholder is logged so the operator can see which
    services are unpopulated.
    """
    if not values:
        logger.warning("{!r}: no Vault values; pushing placeholder Modal Secret.", secret_name)
        values = {_PLACEHOLDER_KEY: _PLACEHOLDER_VALUE}
    command = [
        "modal",
        "secret",
        "create",
        "--force",
        "--env",
        modal_env,
        secret_name,
        *(f"{k}={v}" for k, v in values.items()),
    ]
    cg = parent_cg.make_concurrency_group(name=f"modal-secret-{secret_name}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MODAL_SECRET_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=_modal_subprocess_env(),
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalDeployError(f"`modal secret create {secret_name}` failed (exit {result.returncode}): {stderr}")


def ensure_modal_env(name: DevEnvName, *, parent_cg: ConcurrencyGroup) -> None:
    """Create the Modal env if it doesn't already exist; otherwise no-op.

    Modal's "already exists" failure has shifted wording across
    versions; both known variants contain the substring ``exist``.
    """
    command = ["modal", "environment", "create", str(name)]
    cg = parent_cg.make_concurrency_group(name=f"modal-env-create-{name}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MODAL_ENV_CREATE_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=_modal_subprocess_env(),
        )
    if result.returncode == 0:
        return
    message = (result.stderr + result.stdout).lower()
    if "exist" in message:
        return
    stderr = result.stderr.strip() or result.stdout.strip()
    raise ModalDeployError(f"`modal environment create {name}` failed (exit {result.returncode}): {stderr}")


def deploy_litellm_proxy(
    *,
    modal_env: str,
    tier: str,
    min_containers: int,
    scaledown_window: int,
    deploy_id: str,
    strategy: DeployStrategy,
    parent_cg: ConcurrencyGroup,
) -> AnyUrl:
    """``modal deploy`` the litellm-proxy app into ``modal_env`` for ``tier``.

    Runs the LiteLLM Prisma schema push FIRST so the deployed proxy never
    starts up against a database missing its ``LiteLLM_VerificationToken``
    / ``LiteLLM_BudgetTable`` / etc. tables. The migration runs as a
    Modal Function in the same app file, sharing the proxy's image and
    secret (so ``DATABASE_URL`` is necessarily the same Postgres the
    proxy will talk to). Idempotent.

    Returns the URL Modal actually assigned to the deployed function
    (parsed from ``modal deploy`` stdout). Honors Modal's hostname-
    truncation behavior, so the returned URL is correct even when the
    natural host exceeds DNS's 63-char limit.

    ``modal_env`` is the Modal environment to deploy into: the activated
    dev env's name for dev-tier deploys, or the tier's stable Modal env
    (``main`` by convention) for staging / production deploys.

    ``min_containers`` controls the deployed function's warm-pool size.
    Threaded into the subprocess env as ``MINDS_LITELLM_PROXY_MIN_CONTAINERS``
    so the modal app picks it up at module load.

    ``scaledown_window`` is the idle-before-scaledown window (seconds);
    threaded as ``MINDS_LITELLM_PROXY_SCALEDOWN_WINDOW``. ``0`` means
    "use Modal's own default".
    """
    app_file = _litellm_app_file()
    with info_span(
        "Running LiteLLM Prisma schema migration against {} "
        "(~30-60s first run while Modal builds the image, ~5-15s thereafter; idempotent)",
        modal_env,
    ):
        _run_modal_function(
            app_file=app_file,
            function_name="migrate_db",
            modal_env=modal_env,
            tier=tier,
            deploy_id=deploy_id,
            parent_cg=parent_cg,
        )
    with info_span("modal deploy llm-{} into env {!r} (strategy={})", tier, modal_env, strategy.value):
        return _deploy_modal_app(
            app_file=app_file,
            app_name=f"llm-{tier}",
            modal_env=modal_env,
            tier=tier,
            deploy_id=deploy_id,
            strategy=strategy,
            extra_env={
                LITELLM_PROXY_MIN_CONTAINERS_ENV_VAR: str(min_containers),
                LITELLM_PROXY_SCALEDOWN_WINDOW_ENV_VAR: str(scaledown_window),
            },
            parent_cg=parent_cg,
        )


def delete_modal_secret(
    *,
    secret_name: str,
    modal_env: str,
    parent_cg: ConcurrencyGroup,
) -> None:
    """``modal secret delete <secret_name> --env=<modal_env>``.

    Used by ``minds env destroy --yes-i-mean-staging`` to remove the
    ``<service>-staging`` Modal Secrets after stopping the apps. For dev
    env destroy this is handled implicitly by ``modal environment
    delete`` (which cascade-deletes everything inside the env), so this
    helper is only needed for tier destroys where the Modal env stays.

    Idempotent: treats "secret not found" / "no such secret" as success
    so re-running ``destroy`` after a partial failure is safe.
    """
    command = ["modal", "secret", "delete", "--env", modal_env, "--yes", secret_name]
    cg = parent_cg.make_concurrency_group(name=f"modal-secret-delete-{secret_name}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MODAL_SECRET_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=_modal_subprocess_env(),
        )
    if result.returncode == 0:
        return
    message = (result.stderr + result.stdout).lower()
    if "not found" in message or "no such" in message or "does not exist" in message:
        logger.info("`modal secret delete {} --env {}`: secret already absent.", secret_name, modal_env)
        return
    stderr = result.stderr.strip() or result.stdout.strip()
    raise ModalDeployError(
        f"`modal secret delete {secret_name} --env {modal_env}` failed (exit {result.returncode}): {stderr}"
    )


def stop_modal_app(
    *,
    app_name: str,
    modal_env: str,
    parent_cg: ConcurrencyGroup,
) -> None:
    """``modal app stop <app_name> --env=<modal_env>``.

    Used by ``minds env destroy --yes-i-mean-staging`` to tear down the
    staging tier's deployed apps. Treats "app not found" / "app already
    stopped" as success so re-running ``destroy`` after a failed first
    pass is safe. Any other non-zero exit raises :class:`ModalDeployError`.
    """
    # ``-y`` skips Modal's interactive confirmation prompt; without it the
    # command aborts with "no interactive terminal detected" whenever it runs
    # without a TTY (auto-recover after a failed deploy, CI, background runs).
    command = ["modal", "app", "stop", "-y", "--env", modal_env, app_name]
    cg = parent_cg.make_concurrency_group(name=f"modal-app-stop-{app_name}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MODAL_SECRET_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=_modal_subprocess_env(),
        )
    if result.returncode == 0:
        return
    # Modal's "no such app" wording has shifted across versions:
    #   - older: "could not find a deployed app named '<name>' ..."
    #   - 1.4.x: "No App with name '<name>' found in the '<env>' environment."
    # Both invariant substrings handled below so destroy stays idempotent.
    message = (result.stderr + result.stdout).lower()
    if "not found" in message or "already stopped" in message or "no such" in message or "no app with name" in message:
        logger.info("`modal app stop {} --env {}`: app already stopped or missing.", app_name, modal_env)
        return
    stderr = result.stderr.strip() or result.stdout.strip()
    raise ModalDeployError(
        f"`modal app stop {app_name} --env {modal_env}` failed (exit {result.returncode}): {stderr}"
    )


def deploy_remote_service_connector(
    *,
    modal_env: str,
    tier: str,
    min_containers: int,
    scaledown_window: int,
    deploy_id: str,
    strategy: DeployStrategy,
    parent_cg: ConcurrencyGroup,
) -> AnyUrl:
    """``modal deploy`` the remote_service_connector app into ``modal_env`` for ``tier``.

    See :func:`deploy_litellm_proxy` for return-value semantics and the
    meaning of ``modal_env``. ``min_containers`` is threaded into the
    subprocess env as ``MINDS_CONNECTOR_MIN_CONTAINERS`` and consumed
    by the modal app at module load. ``scaledown_window`` (idle seconds
    before scaledown; ``0`` = Modal default) is threaded as
    ``MINDS_CONNECTOR_SCALEDOWN_WINDOW``.

    ``deploy_id`` is threaded into the subprocess env as ``MINDS_DEPLOY_ID``
    so the deployed connector attaches to the matching ``<svc>-<tier>-<id>``
    Modal Secrets minted by this deploy. Missing the id at the app's module
    load is a hard failure (the app raises ``DeployIdMissingError``).
    """
    with info_span("modal deploy rsc-{} into env {!r} (strategy={})", tier, modal_env, strategy.value):
        return _deploy_modal_app(
            app_file=_connector_app_file(),
            app_name=f"rsc-{tier}",
            modal_env=modal_env,
            tier=tier,
            deploy_id=deploy_id,
            strategy=strategy,
            extra_env={
                CONNECTOR_MIN_CONTAINERS_ENV_VAR: str(min_containers),
                CONNECTOR_SCALEDOWN_WINDOW_ENV_VAR: str(scaledown_window),
            },
            parent_cg=parent_cg,
        )


def _parse_deploy_url_from_stdout(stdout: str) -> AnyUrl | None:
    """Extract the deployed function URL from ``modal deploy`` stdout.

    Modal wraps long URLs across lines in TTY-style output; collapsing
    whitespace before matching catches both wrapped and inline forms.
    Returns the last ``.modal.run`` URL seen (the deployed function);
    earlier matches may be Modal dashboard URLs that aren't useful here.
    Returns ``None`` if no URL is present (the caller decides whether
    that's fatal).
    """
    collapsed = re.sub(r"\s+", "", stdout)
    matches = _MODAL_DEPLOY_URL_PATTERN.findall(collapsed)
    if not matches:
        return None
    return AnyUrl(matches[-1])


def _deploy_modal_app(
    *,
    app_file: Path,
    app_name: str,
    modal_env: str,
    tier: str,
    deploy_id: str,
    strategy: DeployStrategy,
    extra_env: dict[str, str] | None = None,
    parent_cg: ConcurrencyGroup,
) -> AnyUrl:
    if not app_file.is_file():
        raise RepoLayoutError(f"Modal app file not found: {app_file}")
    # Modal's CLI calls the rollover strategy ``rolling`` and the
    # recreate strategy ``recreate``. We use ``ROLLOVER`` in our own
    # vocabulary (matches the operator-facing ``--soft`` flag) and
    # only translate at the CLI boundary.
    modal_strategy_arg = _MODAL_STRATEGY_ARG_BY_ENUM[strategy]
    command = [
        "modal",
        "deploy",
        "--name",
        app_name,
        "--env",
        modal_env,
        "--strategy",
        modal_strategy_arg,
        str(app_file),
    ]
    subprocess_env = _modal_subprocess_env()
    # The Modal apps read MNGR_DEPLOY_ENV + MINDS_DEPLOY_ID at module
    # load to build their ``Secret.from_name(f"<svc>-<tier>-<id>")``
    # calls. Both are baked into the deployment spec at modal-deploy
    # serialization time, so threading them in via the subprocess env
    # is the only way the deployed function spec picks them up.
    subprocess_env["MNGR_DEPLOY_ENV"] = tier
    subprocess_env[MINDS_DEPLOY_ID_ENV_VAR] = deploy_id
    # Extra env vars (e.g. per-app ``MINDS_*_MIN_CONTAINERS``) are
    # baked into the deployment spec at module load -- threading them
    # in via the subprocess env is the only way modal's deploy-time
    # serialization sees them.
    if extra_env is not None:
        subprocess_env.update(extra_env)
    cg = parent_cg.make_concurrency_group(name=f"modal-deploy-{app_name}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MODAL_DEPLOY_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=subprocess_env,
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalDeployError(
            f"`modal deploy --name {app_name} --env {modal_env}` failed (exit {result.returncode}): {stderr}"
        )
    url = _parse_deploy_url_from_stdout(result.stdout)
    if url is None:
        raise ModalDeployError(
            f"`modal deploy --name {app_name} --env {modal_env}` succeeded but no .modal.run URL "
            f"appeared in its stdout. Captured tail: {result.stdout[-500:]}"
        )
    return url


def _modal_subprocess_env() -> dict[str, str]:
    """Build the env modal subprocesses inherit -- just os.environ verbatim.

    Kept as a helper so future plumbing (e.g. injecting a CI token) has
    one place to land.
    """
    return dict(os.environ)


def _run_modal_function(
    *,
    app_file: Path,
    function_name: str,
    modal_env: str,
    tier: str,
    deploy_id: str,
    parent_cg: ConcurrencyGroup,
) -> None:
    """Invoke a Modal Function defined in ``app_file`` via ``modal run``.

    Used by :func:`deploy_litellm_proxy` to run ``migrate_db`` before
    the proxy deploy. ``modal run`` of an ``@app.function`` does not
    require a prior ``modal deploy``: Modal builds an ephemeral instance
    on demand, so this works on first-time tier bootstrap when no
    ``llm-<tier>`` app yet exists.

    ``MNGR_DEPLOY_ENV`` + ``MINDS_DEPLOY_ID`` are set in the subprocess
    env so the Modal app reads them at module load and attaches to the
    correct ``litellm-<tier>-<deploy_id>`` Secret. The just-pushed Secret
    must exist before this runs.
    """
    if not app_file.is_file():
        raise RepoLayoutError(f"Modal app file not found: {app_file}")
    command = [
        "modal",
        "run",
        "--env",
        modal_env,
        f"{app_file}::{function_name}",
    ]
    subprocess_env = _modal_subprocess_env()
    subprocess_env["MNGR_DEPLOY_ENV"] = tier
    subprocess_env[MINDS_DEPLOY_ID_ENV_VAR] = deploy_id
    cg = parent_cg.make_concurrency_group(name=f"modal-run-{function_name}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MODAL_DEPLOY_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=subprocess_env,
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalDeployError(
            f"`modal run --env {modal_env} {app_file}::{function_name}` failed (exit {result.returncode}): {stderr}"
        )


def get_modal_app_latest_version(*, app_name: str, modal_env: str, parent_cg: ConcurrencyGroup) -> str | None:
    """Return the latest deployed version id of ``app_name`` in ``modal_env``, or None.

    Shells out to ``modal app history --env=<env> <app> --json`` and
    parses the first entry. Returns ``None`` if the app has never been
    deployed (Modal returns "app not found" on stderr and exits non-zero),
    so callers can distinguish first-deploy from upgrade-deploy without
    raising.
    """
    command = ["modal", "app", "history", "--env", modal_env, "--json", app_name]
    cg = parent_cg.make_concurrency_group(name=f"modal-app-history-{app_name}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MODAL_SECRET_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=_modal_subprocess_env(),
        )
    if result.returncode != 0:
        message = (result.stderr + result.stdout).lower()
        # Modal CLI's "no such app" wording has shifted across versions:
        #   - older: "could not find a deployed app named '<name>' in the '<env>' environment."
        #   - 1.4.x: "No App with name '<name>' found in the '<env>' environment."
        # Both reduce to a few invariant substrings. Match defensively.
        if (
            "could not find" in message
            or "not found" in message
            or "no such" in message
            or "does not exist" in message
            or "no app with name" in message
        ):
            return None
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalDeployError(
            f"`modal app history {app_name} --env {modal_env}` failed (exit {result.returncode}): {stderr}"
        )
    try:
        rows = json.loads(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ModalDeployError(f"`modal app history --json` returned non-JSON: {exc}") from exc
    if not isinstance(rows, list) or not rows:
        return None
    # Modal sorts history newest-first. Look for a "version" / "Version"
    # field on the first entry.
    first = rows[0]
    if isinstance(first, dict):
        for key in ("Version", "version"):
            value = first.get(key)
            if isinstance(value, str | int):
                return str(value)
    return None


def rollback_modal_app(*, app_name: str, version: str, modal_env: str, parent_cg: ConcurrencyGroup) -> None:
    """``modal app rollback <app> <version> --env=<env>`` + force-terminate prior containers.

    Re-deploys the version that was active at ``version``, including the
    env vars (notably ``MINDS_DEPLOY_ID``) captured at that deploy time
    -- which re-attaches the rolled-back app to the matching
    ``<svc>-<tier>-<id>`` Modal Secrets minted under that prior deploy.
    Idempotent in the sense that re-running with the same target version
    is just a no-op redeploy.

    Modal's ``app rollback`` CLI has no ``--strategy`` flag and always
    uses rollover semantics: the new (rolled-back-spec) deployment goes
    live while containers from the prior (broken) deploy keep serving
    until they idle out. That is the wrong shape for ``minds env
    recover`` -- by the time recover fires, we already know the prior
    deploy was broken, so we want it gone ASAP, not gradually. We
    follow the rollback with an immediate ``modal container stop`` of
    every container belonging to this app; Modal sends SIGINT and
    reschedules any in-flight inputs on cold-boots of the rolled-back
    spec, so within seconds every served request is going to the
    correct (rolled-back) code instead of the broken one.
    """
    command = ["modal", "app", "rollback", "--env", modal_env, app_name, version]
    cg = parent_cg.make_concurrency_group(name=f"modal-app-rollback-{app_name}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MODAL_DEPLOY_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=_modal_subprocess_env(),
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalDeployError(
            f"`modal app rollback {app_name} {version} --env {modal_env}` failed (exit {result.returncode}): {stderr}"
        )

    terminate_modal_app_containers(app_name=app_name, modal_env=modal_env, parent_cg=parent_cg)


def terminate_modal_app_containers(*, app_name: str, modal_env: str, parent_cg: ConcurrencyGroup) -> None:
    """Force-terminate every running container for ``app_name`` in ``modal_env``.

    Used by :func:`rollback_modal_app` to convert Modal's rollover-style
    rollback into recreate-style behavior: after the rollback's new
    function spec goes live, we still need to kill the prior deploy's
    warm containers so the next request cold-boots into the rolled-back
    code instead of the broken one.

    Two-step: ``modal app list --json`` to resolve the app's id (the
    ``modal container list --app-id`` filter requires an id, not a name),
    then ``modal container list --app-id <id> --env <env> --json`` to
    enumerate containers, then ``modal container stop -y <container-id>``
    on each. Container stop sends SIGINT; Modal handles graceful drain +
    reschedules any in-flight inputs to fresh cold-boots of the live
    (rolled-back) spec.

    Idempotent: an empty container list (everything already drained) is
    a no-op. A missing app is treated as success (recover is called from
    failure paths where the app may already have been stopped). Per-
    container stop failures are logged but do not raise -- the
    operator's recover-time priority is "no broken containers serving
    traffic", not "every container stopped cleanly".
    """
    app_id = _find_modal_app_id(app_name=app_name, modal_env=modal_env, parent_cg=parent_cg)
    if app_id is None:
        logger.info(
            "terminate_modal_app_containers: no app named {!r} in env {!r}; nothing to do.", app_name, modal_env
        )
        return

    container_ids = _list_modal_app_container_ids(app_id=app_id, modal_env=modal_env, parent_cg=parent_cg)
    if not container_ids:
        logger.info("terminate_modal_app_containers: app {!r} has no running containers.", app_name)
        return

    logger.info(
        "terminate_modal_app_containers: stopping {} container(s) for app {!r} in env {!r}",
        len(container_ids),
        app_name,
        modal_env,
    )
    cg = parent_cg.make_concurrency_group(name=f"modal-container-stop-{app_name}")
    with cg:
        for container_id in container_ids:
            stop_result = cg.run_process_to_completion(
                command=["modal", "container", "stop", "-y", container_id],
                timeout=_MODAL_SECRET_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=_modal_subprocess_env(),
            )
            if stop_result.returncode != 0:
                stderr = stop_result.stderr.strip() or stop_result.stdout.strip()
                logger.warning(
                    "modal container stop {} failed (exit {}): {}; continuing.",
                    container_id,
                    stop_result.returncode,
                    stderr,
                )


def _find_modal_app_id(*, app_name: str, modal_env: str, parent_cg: ConcurrencyGroup) -> str | None:
    """Resolve ``app_name`` to its Modal app id via ``modal app list --json``, or return None."""
    cg = parent_cg.make_concurrency_group(name=f"modal-app-list-{app_name}")
    with cg:
        result = cg.run_process_to_completion(
            command=["modal", "app", "list", "--env", modal_env, "--json"],
            timeout=_MODAL_SECRET_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=_modal_subprocess_env(),
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalDeployError(
            f"`modal app list --env {modal_env} --json` failed (exit {result.returncode}): {stderr}"
        )
    try:
        rows = json.loads(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ModalDeployError(f"`modal app list --json` returned non-JSON: {exc}") from exc
    if not isinstance(rows, list):
        return None
    # Modal's column names have shifted across versions; check the common shapes
    # for both name and id. Skip stopped apps so we don't try to stop containers
    # for an already-terminated app.
    for row in rows:
        if not isinstance(row, dict):
            continue
        name_value = row.get("Name") or row.get("name") or row.get("App")
        state_value = (row.get("State") or row.get("state") or "").lower()
        if name_value != app_name or "stop" in state_value:
            continue
        for id_key in ("App ID", "app_id", "AppID", "ID", "id"):
            id_value = row.get(id_key)
            if isinstance(id_value, str) and id_value:
                return id_value
    return None


def _list_modal_app_container_ids(*, app_id: str, modal_env: str, parent_cg: ConcurrencyGroup) -> tuple[str, ...]:
    """Return the container ids currently running for ``app_id`` in ``modal_env``."""
    cg = parent_cg.make_concurrency_group(name=f"modal-container-list-{app_id}")
    with cg:
        result = cg.run_process_to_completion(
            command=["modal", "container", "list", "--env", modal_env, "--app-id", app_id, "--json"],
            timeout=_MODAL_SECRET_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=_modal_subprocess_env(),
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalDeployError(
            f"`modal container list --env {modal_env} --app-id {app_id} --json` "
            f"failed (exit {result.returncode}): {stderr}"
        )
    try:
        rows = json.loads(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ModalDeployError(f"`modal container list --json` returned non-JSON: {exc}") from exc
    if not isinstance(rows, list):
        return ()
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for id_key in ("Container ID", "container_id", "ID", "id"):
            value = row.get(id_key)
            if isinstance(value, str) and value:
                ids.append(value)
                break
    return tuple(ids)


def per_env_secret_services() -> tuple[str, ...]:
    """Public accessor for the list of services that need per-env Modal Secrets."""
    return _PER_ENV_SECRET_SERVICES


def compute_per_env_overrides(
    name: DevEnvName,
    *,
    modal_workspace: str,
    tier: str,
    neon_record: NeonProjectRecord,
    supertokens_record: SuperTokensAppRecord,
) -> dict[str, dict[str, str]]:
    """Return per-service Modal Secret value overrides for this dev env.

    Keys missing from the result inherit the tier-shared Vault value
    verbatim (or fall through to a placeholder if no tier value exists).

    Both ``neon.DATABASE_URL`` (consumed by the connector for pool host
    rows) and ``litellm.DATABASE_URL`` (consumed by the LiteLLM proxy
    for spend tracking + virtual keys) get overridden to point at the
    per-env Neon project's two databases. The tier-shared vault values
    for those keys are intentionally bypassed; only their non-DSN
    fields (e.g. ``LITELLM_MASTER_KEY``, ``ANTHROPIC_API_KEY``) survive
    the merge into the per-env Modal Secret.
    """
    connector_url = per_env_connector_url(name, modal_workspace, tier=tier)
    proxy_url = per_env_litellm_proxy_url(name, modal_workspace, tier=tier)
    return {
        "supertokens": {
            "SUPERTOKENS_CONNECTION_URI": supertokens_record.connection_uri,
            "AUTH_WEBSITE_DOMAIN": str(connector_url),
        },
        "neon": {
            "DATABASE_URL": neon_record.host_pool_dsn.get_secret_value(),
        },
        "litellm": {
            "DATABASE_URL": neon_record.litellm_cost_dsn.get_secret_value(),
        },
        "litellm-connector": {
            "LITELLM_PROXY_URL": str(proxy_url),
        },
    }
