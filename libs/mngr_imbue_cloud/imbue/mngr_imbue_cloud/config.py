import os
import tomllib
from pathlib import Path

from pydantic import AnyUrl
from pydantic import Field

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_imbue_cloud.errors import ImbueCloudError
from imbue.mngr_imbue_cloud.primitives import IMBUE_CLOUD_BACKEND_NAME
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_vps.config import VpsProviderConfig

CONNECTOR_URL_ENV_VAR = "MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL"

# Base URL of the tier's browser accounts origin (e.g. https://accounts.imbue.com
# on production) -- the host where the hosted login/signup pages actually work:
# Google's OAuth redirect URI is registered there, and the browser session
# cookie's Domain is scoped to its apex. Only `auth login` (which opens a
# browser) consumes this; API traffic stays on the connector URL. Unset means
# the tier has no dedicated accounts origin (dev/CI) and the connector host
# serves the pages itself.
ACCOUNTS_URL_ENV_VAR = "MNGR__PROVIDERS__IMBUE_CLOUD__ACCOUNTS_URL"


class MissingConnectorUrlError(ImbueCloudError):
    """Raised when the provider's connector URL is unset (no field, no env)."""


class ImbueCloudProviderConfig(VpsProviderConfig):
    """Configuration for an imbue_cloud provider instance.

    Extends ``VpsProviderConfig`` so it carries the runtime knobs
    (``docker_runtime`` / ``install_gvisor_runtime`` / ``default_start_args``)
    that the slow (rebuild) path forwards onto the delegated ``vps_docker``
    provider in ``ImbueCloudProvider._build_delegated_vps_provider``. Minds
    bootstrap writes runsc + hardening values into the per-account block (see
    ``minds.bootstrap.set_imbue_cloud_provider_for_account``).

    Two recognized usages:

    - Default instance ``[providers.imbue_cloud]``: ``account`` is unset.
      The provider falls back to the active account written by
      ``mngr imbue_cloud auth use --account <email>`` (set automatically
      whenever an account signs in). Callers can still pin per-call via
      ``-b account=<email>`` on ``mngr create``.
    - Per-account instance ``[providers.imbue_cloud_<slug>]``: ``account``
      is bound at config time. Minds writes one of these per signed-in
      account into its mngr settings.toml (see
      ``minds.bootstrap.set_imbue_cloud_provider_for_account`` /
      ``unset_imbue_cloud_provider_for_account``) so per-account
      ``discover_hosts`` works without consulting the active-account file.
    """

    backend: ProviderBackendName = Field(
        default=ProviderBackendName(IMBUE_CLOUD_BACKEND_NAME),
        description="Always 'imbue_cloud' for this backend",
    )
    account: ImbueCloudAccount | None = Field(
        default=None,
        description=(
            "Email of the Imbue Cloud account this provider instance is bound to. "
            "Optional; when unset, the provider uses the active account (see "
            "``mngr imbue_cloud auth use``) or accepts ``-b account=<email>`` on ``mngr create``."
        ),
    )
    connector_url: AnyUrl | None = Field(
        default=None,
        description=(
            "Override for the remote_service_connector base URL. When None, the plugin reads "
            "MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL from the environment. There is no "
            "baked-in default; raise when neither source supplies a value."
        ),
    )
    accounts_url: AnyUrl | None = Field(
        default=None,
        description=(
            "Base URL of the tier's browser accounts origin (e.g. https://accounts.imbue.com). "
            "Declared so the MNGR__PROVIDERS__IMBUE_CLOUD__ACCOUNTS_URL env override is a valid "
            "config key (mngr parses every MNGR__PROVIDERS__* env var as a provider-config "
            "override and rejects unknown fields). `auth login` resolves the value from its "
            "--accounts-url flag or that env var directly, not from this field: plugin-local "
            "CLI commands run without a loaded mngr config. None means the tier has no "
            "dedicated accounts origin and the login page opens on the connector host."
        ),
    )
    container_ssh_port: int = Field(
        default=2222,
        description="Port that maps to sshd inside the leased docker container",
    )
    host_dir: Path = Field(
        default=Path("/mngr"),
        description="Base directory for mngr data inside the leased container (matches the pool-host convention)",
    )

    def get_connector_url(self) -> str:
        """Resolve the effective connector URL.

        Precedence: per-instance ``connector_url`` field >
        ``MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL`` env. There is no
        baked-in default; raises :class:`MissingConnectorUrlError` if
        neither source is set.
        """
        if self.connector_url is not None:
            return str(self.connector_url).rstrip("/")
        env_value = os.environ.get(CONNECTOR_URL_ENV_VAR)
        if env_value:
            return env_value.rstrip("/")
        raise MissingConnectorUrlError(
            "No connector URL configured for the imbue_cloud provider: set `connector_url` "
            f"on the provider config or export ${CONNECTOR_URL_ENV_VAR}."
        )


def get_active_profile_dir(default_host_dir: Path) -> Path:
    """Resolve the active mngr profile dir (``<host_dir>/profiles/<id>``).

    Used by plugin CLI subcommands that don't have a full ``MngrContext``
    (the ``mngr_ctx.profile_dir`` route is preferred whenever a context is
    available, e.g. inside ``ImbueCloudProvider`` methods).

    Raises ``ImbueCloudError`` if mngr hasn't been initialized in this
    host_dir yet -- there's nothing to attach plugin state to -- and for an
    unreadable or malformed root config, so callers (including a desktop
    client's render-path identity cache) only ever have to handle
    ``ImbueCloudError``.
    """
    expanded = default_host_dir.expanduser()
    config_path = expanded / "config.toml"
    if not config_path.exists():
        raise ImbueCloudError(
            f"mngr root config not found at {config_path}; run any `mngr` command once to initialize."
        )
    try:
        root_config = tomllib.loads(config_path.read_text())
    except OSError as exc:
        raise ImbueCloudError(f"Failed to read mngr root config at {config_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ImbueCloudError(f"mngr root config at {config_path} is not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ImbueCloudError(f"mngr root config at {config_path} is not valid TOML: {exc}") from exc
    profile_id = root_config.get("profile")
    if not isinstance(profile_id, str) or not profile_id:
        raise ImbueCloudError(
            f"mngr root config at {config_path} has no valid `profile` entry; reinitialize with `mngr config init`."
        )
    return expanded / "profiles" / profile_id


def get_provider_state_dir(profile_dir: Path) -> Path:
    """Root of the imbue_cloud plugin's on-disk state for ``profile_dir``.

    Mirrors the convention every other provider follows: profile-scoped
    state lives at ``<profile_dir>/providers/<backend>/``. Sessions and
    the active-account marker live one level down (shared across
    instances); per-instance state (per-host SSH keys, lease.json caches)
    lives under ``./<instance_name>/``.
    """
    return profile_dir / "providers" / IMBUE_CLOUD_BACKEND_NAME


def get_provider_data_dir(profile_dir: Path, instance_name: str) -> Path:
    """Per-provider-instance state dir, e.g. lease keypairs + caches."""
    return get_provider_state_dir(profile_dir) / instance_name


def get_sessions_dir(profile_dir: Path) -> Path:
    """Sessions are shared across all imbue_cloud instances (keyed by user_id)."""
    return get_provider_state_dir(profile_dir) / "sessions"
