import os
from enum import auto
from typing import Final

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vps.config import VpsProviderConfig

_DEFAULT_ENDPOINT: Final[str] = "ovh-us"
_DEFAULT_PLAN: Final[str] = "vps-2025-model1"
_DEFAULT_REGION: Final[str] = "US-EAST-VA"
_DEFAULT_IMAGE_NAME: Final[str] = "Debian 12 - Docker"
# OVH images install the rebuild SSH key into the image's default non-root
# user, not into /root. mngr operates as root downstream so we sudo-copy the
# key to root during provisioning; this is the user the rebuild key lands on.
_DEFAULT_BOOTSTRAP_SSH_USER: Final[str] = "debian"


class OvhPricingMode(UpperCaseStrEnum):
    """OVH cart pricing modes for VPS orders."""

    DEFAULT = auto()
    UPFRONT6 = auto()
    UPFRONT12 = auto()

    def to_wire_value(self) -> str:
        """Return the lowercase string OVH's order/cart API expects."""
        return self.value.lower()


class OvhProviderConfig(VpsProviderConfig):
    """Configuration for the OVH classic-VPS Docker provider."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("ovh"),
        description="Provider backend (always 'ovh' for this type)",
    )
    endpoint: str = Field(
        default=_DEFAULT_ENDPOINT,
        description="python-ovh endpoint id ('ovh-eu', 'ovh-ca', ...). Falls back to OVH_ENDPOINT.",
    )
    application_key: SecretStr | None = Field(
        default=None,
        description="OVH application key (AK). Falls back to OVH_APPLICATION_KEY/OVH_APP_KEY env vars or ~/.ovh.conf.",
    )
    application_secret: SecretStr | None = Field(
        default=None,
        description="OVH application secret (AS). Falls back to OVH_APPLICATION_SECRET/OVH_APP_SECRET env vars or ~/.ovh.conf.",
    )
    consumer_key: SecretStr | None = Field(
        default=None,
        description="OVH consumer key (CK). Falls back to OVH_CONSUMER_KEY env var or ~/.ovh.conf.",
    )
    client_id: SecretStr | None = Field(
        default=None,
        description="OVH OAuth2 client id. Falls back to OVH_CLIENT_ID env var or ~/.ovh.conf.",
    )
    client_secret: SecretStr | None = Field(
        default=None,
        description="OVH OAuth2 client secret. Falls back to OVH_CLIENT_SECRET env var or ~/.ovh.conf.",
    )
    project_id: str | None = Field(
        default=None,
        description="OVH cloud project ID. Reserved for future Public Cloud support; unused for classic VPS.",
    )
    default_region: str = Field(
        default=_DEFAULT_REGION,
        description="Default VPS datacenter (e.g. US-WEST-OR for US accounts).",
    )
    default_plan: str = Field(
        default=_DEFAULT_PLAN,
        description="Default VPS plan code (1 vCPU / 8 GB RAM / 80 GB SSD, ~$7.99/mo).",
    )
    default_image_name: str = Field(
        default=_DEFAULT_IMAGE_NAME,
        description="Default OS image name (Docker pre-installed).",
    )
    bootstrap_ssh_user: str = Field(
        default=_DEFAULT_BOOTSTRAP_SSH_USER,
        description=(
            "Non-root user the OVH image installs the rebuild key for. "
            "Override only if you change default_image_name to a non-Debian "
            "image (e.g. ubuntu, almalinux)."
        ),
    )
    pricing_mode: OvhPricingMode = Field(
        default=OvhPricingMode.DEFAULT,
        description="OVH pricing mode. UPFRONT6 / UPFRONT12 get a discount in exchange for prepayment.",
    )
    duration: str = Field(
        default="P1M",
        description="ISO-8601 commitment duration. OVH classic VPS only supports monthly billing.",
    )
    instance_boot_timeout: float = Field(
        default=600.0,
        description="Seconds to wait for the OVH order to deliver a VPS.",
    )
    ovh_subsidiary: str = Field(
        default="US",
        description="OVHcloud subsidiary code used for ordering. Must match the account region.",
    )
    enable_recycle_cancelled: bool = Field(
        default=True,
        description=("Whether `mngr create` may reuse a cancelled-but-still-alive VPS instead of ordering fresh."),
    )
    recycle_safety_margin_hours: int = Field(
        default=2,
        description=("Minimum hours of remaining expiration for a cancelled VPS to be recyclable."),
    )
    recycle_max_candidates_considered: int = Field(
        default=10,
        description=(
            "Cap on provider-tagged VPSes evaluated before falling through to a "
            "fresh order. Applied to the raw tagged-VPS list before the "
            "cancellation/state/expiration filters run, so on accounts with many "
            "active mngr-tagged VPSes a recyclable candidate further down the "
            "list may be missed."
        ),
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

        Does not raise when credentials are missing -- the resulting
        kwargs may contain only ``endpoint``, leaving ``ovh.Client`` to
        either pick credentials up from ``~/.ovh.conf`` or raise
        ``ovh.exceptions.InvalidConfiguration`` itself at construction
        time (which the backend handles by substituting placeholders).
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
