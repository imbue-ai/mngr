"""The logins installed on this machine that a local-profile connection can name.

Read straight from each tool's own files (no subprocess, no network) so the
add-connection modal can list them instantly: AWS profiles from
``~/.aws/config`` and ``~/.aws/credentials`` through botocore's loader, Azure
CLI subscriptions from ``azureProfile.json``, Modal profiles from
``~/.modal.toml``. Each reader honors the environment variable its tool uses
to move the file.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import assert_never

import botocore.session
from botocore.exceptions import ConfigParseError
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.mngr_azure.config import az_cli_profile_path
from imbue.mngr_azure.config import read_az_profile_subscriptions
from imbue.mngr_modal.config import modal_config_path
from imbue.mngr_modal.config import read_modal_profiles


class LocalCredentialProfile(FrozenModel):
    """One login on this machine a connection can authenticate through."""

    name: str = Field(description="What the connection names: the AWS or Modal profile, or the Azure subscription id")
    label: str = Field(description="How the modal lists it")
    region: str | None = Field(default=None, description="The AWS profile's own region, to seed the region picker")
    tenant_id: str | None = Field(default=None, description="The Azure subscription's tenant")
    subscription_id: str | None = Field(default=None, description="The Azure subscription id")
    unusable_reason: str = Field(
        default="", description="Why a connection cannot use this login (an AWS mfa_serial), empty when it can"
    )


def _aws_file_paths() -> tuple[Path, Path]:
    """This machine's AWS config and credentials files, as botocore resolves them (env vars included)."""
    session = botocore.session.Session()
    return (
        Path(session.get_config_variable("config_file")).expanduser(),
        Path(session.get_config_variable("credentials_file")).expanduser(),
    )


def list_aws_profiles(config_path: Path, credentials_path: Path) -> list[LocalCredentialProfile]:
    """Every named profile in the two AWS files, in name order, with the ones mngr cannot use marked.

    botocore merges the two files the way the AWS CLI reads them (``[profile X]``
    in the config file, bare ``[X]`` in the credentials file), so its loader is
    the one parser of them; a malformed file lists nothing.
    """
    session = botocore.session.Session()
    session.set_config_variable("config_file", str(config_path))
    session.set_config_variable("credentials_file", str(credentials_path))
    try:
        settings_by_name: Mapping[str, Mapping[str, Any]] = session.full_config["profiles"]
    except ConfigParseError as e:
        logger.warning("Could not parse the AWS config files to list their profiles: {}", e)
        return []
    profiles: list[LocalCredentialProfile] = []
    for name in sorted(settings_by_name):
        settings = settings_by_name[name]
        is_mfa = bool(settings.get("mfa_serial"))
        profiles.append(
            LocalCredentialProfile(
                name=name,
                label=name,
                region=settings.get("region") or None,
                unusable_reason="needs an MFA code on every use, which the app cannot enter" if is_mfa else "",
            )
        )
    return profiles


def list_modal_profiles(config_path: Path) -> list[LocalCredentialProfile]:
    """Every ``~/.modal.toml`` section that holds a token pair, in file order, through mngr_modal's reader."""
    try:
        profiles = read_modal_profiles(config_path)
    except FileNotFoundError:
        return []
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read {} to list its profiles: {}", config_path, e)
        return []
    return [LocalCredentialProfile(name=name, label=name) for name in profiles]


def list_azure_cli_subscriptions(profile_path: Path) -> list[LocalCredentialProfile]:
    """Every enabled subscription the Azure CLI login on this machine can act in, default first."""
    try:
        subscriptions = read_az_profile_subscriptions(profile_path)
    except FileNotFoundError:
        return []
    if subscriptions is None:
        logger.warning("Could not read {} to list the az login's subscriptions", profile_path)
        return []
    if not isinstance(subscriptions, list):
        return []
    profiles: list[LocalCredentialProfile] = []
    for subscription in subscriptions:
        if not isinstance(subscription, dict) or subscription.get("state", "Enabled") != "Enabled":
            continue
        subscription_id = subscription.get("id")
        tenant_id = subscription.get("tenantId")
        if not isinstance(subscription_id, str) or not subscription_id or not isinstance(tenant_id, str):
            continue
        display_name = str(subscription.get("name") or subscription_id)
        profiles.append(
            LocalCredentialProfile(
                name=subscription_id,
                label=f"{display_name} ({subscription_id})",
                tenant_id=tenant_id,
                subscription_id=subscription_id,
            )
        )
    profiles.sort(key=lambda profile: 0 if _is_default_subscription(subscriptions, profile.name) else 1)
    return profiles


@pure
def _is_default_subscription(subscriptions: list[Any], subscription_id: str) -> bool:
    return any(
        isinstance(subscription, dict)
        and subscription.get("id") == subscription_id
        and subscription.get("isDefault") is True
        for subscription in subscriptions
    )


def list_local_credential_profiles(backend: ConnectionBackend) -> list[LocalCredentialProfile]:
    """The logins on this machine a connection of ``backend`` may name; empty for a backend that has none."""
    match backend:
        case ConnectionBackend.AWS:
            return list_aws_profiles(*_aws_file_paths())
        case ConnectionBackend.AZURE:
            return list_azure_cli_subscriptions(az_cli_profile_path())
        case ConnectionBackend.MODAL:
            return list_modal_profiles(modal_config_path())
        case ConnectionBackend.GCP:
            return []
        case _ as unreachable:
            assert_never(unreachable)
