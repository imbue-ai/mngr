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

import os
import re
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import AnyUrl

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.providers.neon_db import NeonDatabaseRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.errors import MindError

# Modal's `modal deploy` prints lines like:
#     Created web function fastapi_app => https://<host>.modal.run
# When the natural host exceeds DNS's 63-char limit, Modal truncates and
# appends a 6-hex hash, and may wrap the URL across stdout lines. Collapsing
# whitespace before regex matching handles both forms.
_MODAL_DEPLOY_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"https://[A-Za-z0-9_\-.]+\.modal\.run")

# Services that need a per-env Modal Secret. Order doesn't matter for the
# pushes themselves, but listing them here in one place keeps the set
# explicit -- each name corresponds to an entry under
# ``.minds/template/<name>.sh`` and a Modal Secret ``<name>-<tier>``.
_PER_ENV_SECRET_SERVICES: Final[tuple[str, ...]] = (
    "litellm",
    "supertokens",
    "cloudflare",
    "neon",
    "pool-ssh",
    "litellm-connector",
    "paid-accounts",
)

# Subset of ``_PER_ENV_SECRET_SERVICES`` whose values are constructed
# entirely at deploy time from other tier secrets + deploy URLs rather
# than read from a Vault entry. ``build_per_env_secret_values`` skips
# the Vault read for these so the deploy log doesn't get a misleading
# "Vault read for litellm-connector failed" warning every time. Keep
# in sync with anything in provisioning.py that supplies overrides for
# a service without ever expecting a Vault-backed base.
_DERIVED_ONLY_SECRET_SERVICES: Final[frozenset[str]] = frozenset({"litellm-connector"})

# Placeholder key written when a Vault entry isn't populated yet. Modal
# requires at least one KEY=VALUE pair to create a Secret; this gives us
# one without exposing anything meaningful, so ``modal deploy`` can still
# reference the Secret.
_PLACEHOLDER_KEY: Final[str] = "MNGR_PLACEHOLDER"
_PLACEHOLDER_VALUE: Final[str] = "unpopulated"

_MODAL_DEPLOY_TIMEOUT_SECONDS: Final[float] = 600.0
_MODAL_SECRET_TIMEOUT_SECONDS: Final[float] = 60.0
_MODAL_ENV_CREATE_TIMEOUT_SECONDS: Final[float] = 60.0


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


def per_env_connector_url(name: DevEnvName, modal_workspace: str) -> AnyUrl:
    """Compute the connector's URL for the given dev env.

    Modal asgi apps follow ``<workspace>--<app>-<function>.modal.run``,
    with the env name embedded as ``<workspace>-<env>--<app>-...``
    (Modal's URL convention for non-default envs).
    """
    return AnyUrl(f"https://{modal_workspace}-{name}--remote-service-connector-dev-fastapi-app.modal.run")


def per_env_litellm_proxy_url(name: DevEnvName, modal_workspace: str) -> AnyUrl:
    return AnyUrl(f"https://{modal_workspace}-{name}--litellm-proxy-dev-litellm-app.modal.run")


def build_per_env_secret_values(
    service: str,
    *,
    tier_vault_prefix: str,
    overrides: dict[str, str],
    parent_cg: ConcurrencyGroup,
) -> dict[str, str]:
    """Read tier-shared values for one service from Vault and layer overrides.

    Services listed in ``_DERIVED_ONLY_SECRET_SERVICES`` skip the Vault
    read entirely -- their values are 100% derived from ``overrides``
    (other secrets + deploy URLs), so a Vault entry is intentionally
    absent and we shouldn't warn about it.

    For Vault-backed services, missing Vault entries return an empty
    dict and emit a warning so the operator can populate them later;
    the caller is expected to fall back to a placeholder when both the
    tier-shared values and overrides come up empty.
    """
    base: dict[str, str] = {}
    if service not in _DERIVED_ONLY_SECRET_SERVICES:
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
    """
    app_file = _litellm_app_file()
    logger.info(
        "Running LiteLLM Prisma schema push against the litellm DATABASE_URL "
        "(this can take ~30-60s on first run while Modal builds the image, "
        "~5-15s thereafter; the push itself is idempotent)..."
    )
    _run_modal_function(
        app_file=app_file,
        function_name="migrate_db",
        modal_env=modal_env,
        tier=tier,
        parent_cg=parent_cg,
    )
    return _deploy_modal_app(
        app_file=app_file,
        app_name=f"litellm-proxy-{tier}",
        modal_env=modal_env,
        tier=tier,
        parent_cg=parent_cg,
    )


def deploy_remote_service_connector(
    *,
    modal_env: str,
    tier: str,
    parent_cg: ConcurrencyGroup,
) -> AnyUrl:
    """``modal deploy`` the remote_service_connector app into ``modal_env`` for ``tier``.

    See :func:`deploy_litellm_proxy` for return-value semantics and the
    meaning of ``modal_env``.
    """
    return _deploy_modal_app(
        app_file=_connector_app_file(),
        app_name=f"remote-service-connector-{tier}",
        modal_env=modal_env,
        tier=tier,
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
    parent_cg: ConcurrencyGroup,
) -> AnyUrl:
    if not app_file.is_file():
        raise RepoLayoutError(f"Modal app file not found: {app_file}")
    command = [
        "modal",
        "deploy",
        "--name",
        app_name,
        "--env",
        modal_env,
        str(app_file),
    ]
    subprocess_env = _modal_subprocess_env()
    # The Modal apps read MNGR_DEPLOY_ENV at module load to pick the
    # right secret names; tier deploys set this in the shell wrapper, so
    # mirror that here.
    subprocess_env["MNGR_DEPLOY_ENV"] = tier
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
    parent_cg: ConcurrencyGroup,
) -> None:
    """Invoke a Modal Function defined in ``app_file`` via ``modal run``.

    Used by :func:`deploy_litellm_proxy` to run ``migrate_db`` before
    the proxy deploy. ``modal run`` of an ``@app.function`` does not
    require a prior ``modal deploy``: Modal builds an ephemeral instance
    on demand, so this works on first-time tier bootstrap when no
    ``litellm-proxy-<tier>`` app yet exists.

    ``MNGR_DEPLOY_ENV`` is set in the subprocess env because the Modal
    app reads it at module load to pick the right per-tier Secret name
    (``litellm-<tier>``). Without it the function would attach the wrong
    secret and either fail to find a DATABASE_URL or, worse, migrate
    against the wrong tier's database.
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


def per_env_secret_services() -> tuple[str, ...]:
    """Public accessor for the list of services that need per-env Modal Secrets."""
    return _PER_ENV_SECRET_SERVICES


def compute_per_env_overrides(
    name: DevEnvName,
    *,
    modal_workspace: str,
    neon_record: NeonDatabaseRecord,
    supertokens_record: SuperTokensAppRecord,
) -> dict[str, dict[str, str]]:
    """Return per-service Modal Secret value overrides for this dev env.

    Keys missing from the result inherit the tier-shared Vault value
    verbatim (or fall through to a placeholder if no tier value exists).
    """
    connector_url = per_env_connector_url(name, modal_workspace)
    proxy_url = per_env_litellm_proxy_url(name, modal_workspace)
    return {
        "supertokens": {
            "SUPERTOKENS_CONNECTION_URI": supertokens_record.connection_uri,
            "AUTH_WEBSITE_DOMAIN": str(connector_url),
        },
        "neon": {
            "DATABASE_URL": neon_record.pooled_dsn.get_secret_value(),
        },
        "litellm-connector": {
            "LITELLM_PROXY_URL": str(proxy_url),
        },
    }
