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

    Missing Vault entries return an empty dict (no error). The caller is
    expected to fall back to a placeholder when both the tier-shared
    values and overrides come up empty.
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
    name: DevEnvName,
    *,
    tier: str,
    parent_cg: ConcurrencyGroup,
) -> None:
    """``modal deploy`` the litellm-proxy app into env ``name`` for ``tier``."""
    _deploy_modal_app(
        app_file=_litellm_app_file(),
        app_name=f"litellm-proxy-{tier}",
        modal_env=str(name),
        tier=tier,
        parent_cg=parent_cg,
    )


def deploy_remote_service_connector(
    name: DevEnvName,
    *,
    tier: str,
    parent_cg: ConcurrencyGroup,
) -> None:
    """``modal deploy`` the remote_service_connector app into env ``name`` for ``tier``."""
    _deploy_modal_app(
        app_file=_connector_app_file(),
        app_name=f"remote-service-connector-{tier}",
        modal_env=str(name),
        tier=tier,
        parent_cg=parent_cg,
    )


def _deploy_modal_app(
    *,
    app_file: Path,
    app_name: str,
    modal_env: str,
    tier: str,
    parent_cg: ConcurrencyGroup,
) -> None:
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


def _modal_subprocess_env() -> dict[str, str]:
    """Build the env modal subprocesses inherit -- just os.environ verbatim.

    Kept as a helper so future plumbing (e.g. injecting a CI token) has
    one place to land.
    """
    return dict(os.environ)


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
