"""Find and delete OVH VPSes belonging to a minds env.

Per the per-env-data-roots refactor, every OVH VPS the pool-host bake
creates for a minds env (dev, staging, or any other) carries
``minds_env=<env-name>`` as an OVH IAM v2 tag. ``minds env destroy``
walks the account's IAM resource list to enumerate / tear down
everything its env owns.

There is no ``create`` operation here -- pool VPSes are still
provisioned via ``mngr imbue_cloud admin pool create``, which is the
layer responsible for applying the tag at instance-create time (it
passes ``MNGR_VPS_EXTRA_TAGS=minds_env=<env-name>`` to the inner
``mngr create``; ``mngr_ovh.OvhProvider`` then attaches the entries
as OVH IAM v2 tags after the VPS is provisioned). This module only
handles discovery + destruction.
"""

from typing import Final

import ovh
from loguru import logger
from ovh.exceptions import InvalidConfiguration
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.iam_tags import IamResource
from imbue.mngr_ovh.iam_tags import list_vps_resources
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.primitives import VpsInstanceId

_TAG_KEY: Final[str] = "minds_env"

# All OVH-account-level operations route through whichever endpoint
# the operator's credentials are scoped to. Today minds only ever bakes
# against ``ovh-us`` (see :mod:`mngr_ovh.config`); revisit if a non-US
# tier ever wants to provision pool hosts.
_DEFAULT_ENDPOINT: Final[str] = "ovh-us"
# OVH classic VPS lives in the ``US`` subsidiary for ``ovh-us``; the
# subsidiary code is used by the ``python-ovh`` client wrapper to
# construct order endpoints. Unused in the listing/destroy paths we
# call below but required for ``OvhVpsClient`` construction.
_DEFAULT_SUBSIDIARY: Final[str] = "US"


class OvhProviderError(MindError):
    """Raised when the OVH API rejects a request."""


class OvhCredentials(FrozenModel):
    """Dev-tier OVH credentials read from the ``<tier>/ovh`` Vault entry.

    AK/AS/CK is the credential scheme ``mngr_ovh`` documents first; the
    OAuth2 path also exists but is operator-installed only (via
    ``~/.ovh.conf``).
    """

    application_key: SecretStr = Field(description="OVH AK (``OVH_APPLICATION_KEY``).")
    application_secret: SecretStr = Field(description="OVH AS (``OVH_APPLICATION_SECRET``).")
    consumer_key: SecretStr = Field(description="OVH CK (``OVH_CONSUMER_KEY``).")


def env_tag_value(name: DevEnvName) -> tuple[str, str]:
    """Return the ``(key, value)`` IAM tag pair applied to VPSes owned by env ``name``."""
    return _TAG_KEY, str(name)


def _build_client(credentials: OvhCredentials) -> OvhVpsClient:
    """Construct an :class:`OvhVpsClient` from the dev-tier Vault credentials.

    Mirrors :func:`mngr_ovh.client.build_ovh_client`, but reads credentials
    from the in-process :class:`OvhCredentials` instead of mngr config /
    env / ``~/.ovh.conf``. Raises :class:`OvhProviderError` if the OVH
    SDK rejects the credentials at construction time.
    """
    try:
        raw_client = ovh.Client(
            endpoint=_DEFAULT_ENDPOINT,
            application_key=credentials.application_key.get_secret_value(),
            application_secret=credentials.application_secret.get_secret_value(),
            consumer_key=credentials.consumer_key.get_secret_value(),
        )
    except InvalidConfiguration as exc:
        raise OvhProviderError(f"OVH credentials rejected at client construction: {exc}") from exc
    return OvhVpsClient(ovh_client=raw_client, subsidiary=_DEFAULT_SUBSIDIARY)


def list_env_instances(
    name: DevEnvName,
    *,
    credentials: OvhCredentials,
) -> tuple[IamResource, ...]:
    """Return every OVH VPS carrying this env's IAM tag.

    Walks the account's full ``/v2/iam/resource?resourceType=vps``
    listing and filters client-side -- the OVH ``?tags[k][value]=v``
    server-side filter is rejected as a 400 by the IAM v2 endpoint
    (verified live; see :func:`mngr_ovh.iam_tags.list_vps_resources`).
    """
    key, value = env_tag_value(name)
    client = _build_client(credentials)
    try:
        all_resources = list_vps_resources(client)
    except VpsApiError as exc:
        raise OvhProviderError(f"OVH IAM resource listing failed: {exc}") from exc
    return tuple(r for r in all_resources if r.tags.get(key) == value)


def delete_instances(
    instances: tuple[IamResource, ...],
    *,
    credentials: OvhCredentials,
) -> None:
    """Terminate every OVH VPS in ``instances`` via ``POST /vps/{s}/terminate``.

    OVH's termination is asynchronous (the VPS keeps billing until the
    end of the current month) and requires an email-confirmed token to
    fully decommission. From our side the VPS is logically destroyed
    after this call; operators clean up the post-cancellation residue
    via the OVH dashboard or by waiting out the billing month.

    Failures on any single VPS abort the loop (callers can re-run
    ``minds env destroy`` to retry). Returns silently when ``instances``
    is empty.
    """
    if not instances:
        return
    client = _build_client(credentials)
    for resource in instances:
        if not resource.name:
            logger.warning("Skipping OVH IAM resource with empty name: urn={}", resource.urn)
            continue
        try:
            client.destroy_instance(VpsInstanceId(resource.name))
        except VpsApiError as exc:
            raise OvhProviderError(f"OVH VPS {resource.name} termination failed: {exc}") from exc
