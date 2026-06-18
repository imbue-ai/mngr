"""Shared helpers for the cloud VPS providers' operator CLIs.

The AWS / Azure / GCP / OVH operator commands (``mngr <cloud> prepare`` /
``cleanup`` / ``list``) each need to resolve the user's ``[providers.<name>]``
settings block and -- for the cleanup commands -- refuse to delete shared
network infrastructure while mngr-managed instances still exist. Those routines
were near-verbatim copies across the four CLIs; they live here so each CLI is a
thin call site.
"""

from collections.abc import Callable
from collections.abc import Collection
from typing import TypeVar

from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps.errors import ManagedResourcesExistError

ProviderConfigT = TypeVar("ProviderConfigT", bound=ProviderInstanceConfig)


def resolve_provider_config(
    mngr_ctx: MngrContext,
    provider_name: str,
    *,
    config_cls: type[ProviderConfigT],
    default_factory: Callable[[], ProviderConfigT],
    cloud_label: str,
    override_hint: str,
) -> ProviderConfigT:
    """Return the user's ``[providers.<provider_name>]`` block, or class defaults.

    The operator commands need to target the same project / region / network /
    account the runtime ``mngr create --provider <provider_name>`` path will use,
    so they read the user's resolved provider config rather than the
    ``default_factory()`` class defaults whenever the named block exists and is a
    ``config_cls``.

    Class defaults are the fallback for the first-run case where the user has not
    yet pinned a provider block; that path is silent because it is the expected
    shape. When the named block exists but is *not* a ``config_cls`` (e.g. the
    user pointed ``[providers.<name>]`` at a different backend), the defaults are
    used and a warning is emitted so the user notices their ``--provider``
    selection did not have the intended effect -- ``override_hint`` names the CLI
    flags that can still drive the run, and ``cloud_label`` names the cloud.

    ``config_cls`` is used for the isinstance match; ``default_factory`` builds
    the fallback (passed separately because the abstract ``ProviderInstanceConfig``
    bound declares ``backend`` required, so a bare ``config_cls()`` is not a valid
    no-arg construction at the type level even though every concrete subclass
    defaults it).
    """
    config = mngr_ctx.config.providers.get(ProviderInstanceName(provider_name))
    if isinstance(config, config_cls):
        return config
    if config is not None:
        logger.warning(
            "Provider {!r} is configured but is not {} (got {}); falling back to {} class defaults. {}",
            provider_name,
            cloud_label,
            type(config).__name__,
            config_cls.__name__,
            override_hint,
        )
    return default_factory()


def refuse_if_managed_resources_exist(
    resource_ids: Collection[str],
    *,
    summary: str,
    resource_noun: str,
    scope_description: str,
    cleanup_command: str,
) -> None:
    """Raise ``ManagedResourcesExistError`` when mngr-managed resources still exist.

    The shared guard at the top of every cloud provider's ``cleanup``: it refuses
    to delete the shared network infrastructure (security group / NSG / firewall
    rule / resource group) while any mngr-managed instance is still present, so
    cleanup can never strand a running agent. A no-op when ``resource_ids`` is
    empty.

    ``summary`` is the caller-formatted ``id (...)`` listing of the blocking
    resources (formatting varies per cloud, e.g. id+state, id+state+zone, or bare
    id); ``resource_noun`` is the singular noun ("instance" / "VM");
    ``scope_description`` names what is being cleaned up ("region us-east-1" /
    "resource group mngr" / "project my-proj"); ``cleanup_command`` is the command
    to re-run after destroying them. The unified ``ManagedResourcesExistError`` (a
    ``MngrError``) renders identically across providers.
    """
    if not resource_ids:
        return
    raise ManagedResourcesExistError(
        f"Refusing to clean up {scope_description}: {len(resource_ids)} mngr-managed "
        f"{resource_noun}(s) still exist: {summary}. Destroy them first with "
        f"`mngr destroy <agent>` (or delete them), then re-run `{cleanup_command}`."
    )
