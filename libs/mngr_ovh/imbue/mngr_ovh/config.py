import os
from typing import Final
from typing import Literal

from pydantic import Field
from pydantic import SecretStr

from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig

_DEFAULT_ENDPOINT: Final[str] = "ovh-us"
_DEFAULT_PLAN: Final[str] = "vps-2025-model1"
_DEFAULT_REGION: Final[str] = "US-EAST-VA"
_DEFAULT_IMAGE_NAME: Final[str] = "Debian 12 - Docker"


class OvhCredentialsError(MngrError):
    """Raised when OVH credentials cannot be resolved."""


class OvhProviderConfig(VpsDockerProviderConfig):
    """Configuration for the OVH classic-VPS Docker provider."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("ovh"),
        description="Provider backend (always 'ovh' for this type)",
    )
    endpoint: str = Field(
        default=_DEFAULT_ENDPOINT,
        description="python-ovh endpoint id ('ovh-eu', 'ovh-us', 'ovh-ca', ...). Falls back to OVH_ENDPOINT.",
    )
    application_key: SecretStr | None = Field(
        default=None,
        description="OVH application key (AK). Falls back to OVH_APPLICATION_KEY or OVH_APP_KEY env vars.",
    )
    application_secret: SecretStr | None = Field(
        default=None,
        description="OVH application secret (AS). Falls back to OVH_APPLICATION_SECRET or OVH_APP_SECRET env vars.",
    )
    consumer_key: SecretStr | None = Field(
        default=None,
        description="OVH consumer key (CK). Falls back to OVH_CONSUMER_KEY env var.",
    )
    client_id: SecretStr | None = Field(
        default=None,
        description="OVH OAuth2 client id. Falls back to OVH_CLIENT_ID env var.",
    )
    client_secret: SecretStr | None = Field(
        default=None,
        description="OVH OAuth2 client secret. Falls back to OVH_CLIENT_SECRET env var.",
    )
    project_id: str | None = Field(
        default=None,
        description="OVH cloud project ID. Reserved for future Public Cloud support; unused for classic VPS.",
    )
    default_region: str = Field(
        default=_DEFAULT_REGION,
        description="Default VPS datacenter (e.g. US-EAST-VA, US-WEST-OR for US accounts).",
    )
    default_plan: str = Field(
        default=_DEFAULT_PLAN,
        description="Default VPS plan code (e.g. vps-2025-model1 for VPS-1, ~$7.60/mo).",
    )
    default_image_name: str = Field(
        default=_DEFAULT_IMAGE_NAME,
        description="Default OS image name (resolved to UUID per-VPS at create time).",
    )
    pricing_mode: Literal["default", "upfront6", "upfront12"] = Field(
        default="default",
        description="OVH pricing mode. 'upfront6' / 'upfront12' get a discount in exchange for prepayment.",
    )
    duration: str = Field(
        default="P1M",
        description="ISO-8601 commitment duration. OVH classic VPS only supports monthly billing.",
    )
    vps_boot_timeout: float = Field(
        default=600.0,
        description="Seconds to wait for an OVH order to deliver a VPS (slower than direct-create APIs).",
    )
    ovh_subsidiary: str = Field(
        default="US",
        description="OVHcloud subsidiary code used for ordering. Must match the account region.",
    )

    def resolve_endpoint(self) -> str:
        """Return the python-ovh endpoint id, applying env-var fallback."""
        env_endpoint = os.environ.get("OVH_ENDPOINT")
        if env_endpoint:
            return env_endpoint
        return self.endpoint

    def resolve_python_ovh_kwargs(self) -> dict[str, str]:
        """Return the keyword arguments to pass to ``ovh.Client(...)``.

        Precedence for each credential field:
        1. Explicit ``mngr`` config value (this Pydantic model).
        2. Documented ``OVH_*`` env var; ``OVH_APPLICATION_KEY``/``OVH_APP_KEY`` and
           ``OVH_APPLICATION_SECRET``/``OVH_APP_SECRET`` are accepted as aliases.
        3. ``~/.ovh.conf`` (python-ovh reads this automatically when a
           credential is absent from the constructor kwargs).

        Raises ``OvhCredentialsError`` only when neither this config, env
        vars, nor ``~/.ovh.conf`` provide *any* credentials at all (i.e.
        when the resulting ``ovh.Client`` would have nothing to sign with).
        """
        kwargs: dict[str, str] = {"endpoint": self.resolve_endpoint()}

        app_key = _pick_secret(self.application_key, ["OVH_APPLICATION_KEY", "OVH_APP_KEY"])
        if app_key is not None:
            kwargs["application_key"] = app_key
        app_secret = _pick_secret(self.application_secret, ["OVH_APPLICATION_SECRET", "OVH_APP_SECRET"])
        if app_secret is not None:
            kwargs["application_secret"] = app_secret
        consumer = _pick_secret(self.consumer_key, ["OVH_CONSUMER_KEY"])
        if consumer is not None:
            kwargs["consumer_key"] = consumer
        client_id = _pick_secret(self.client_id, ["OVH_CLIENT_ID"])
        if client_id is not None:
            kwargs["client_id"] = client_id
        client_secret = _pick_secret(self.client_secret, ["OVH_CLIENT_SECRET"])
        if client_secret is not None:
            kwargs["client_secret"] = client_secret
        return kwargs

    def has_explicit_credentials(self) -> bool:
        """Return True iff *some* credential is provided via config or env.

        Used to decide whether to bother instantiating an ``ovh.Client``: if
        no credential is set anywhere, discovery/listing operations are
        expected to silently no-op rather than crash.
        """
        kwargs = self.resolve_python_ovh_kwargs()
        return any(k != "endpoint" for k in kwargs)


def _pick_secret(value: SecretStr | None, env_var_names: list[str]) -> str | None:
    """Resolve a credential from an explicit ``SecretStr`` or any of the env vars."""
    if value is not None:
        return value.get_secret_value()
    for name in env_var_names:
        raw = os.environ.get(name)
        if raw:
            return raw
    return None
