"""Fixtures for the OVH provider unit tests.

Provides an offline ``OvhProvider`` factory so behavior-level tests can
drive ``_provision_vps`` / ``_reconcile_pending_orders`` /
``_maybe_claim_recycled_vps`` against a fake OVH client -- no real OVH
credentials, network, or SSH. Project-wide hooks come from the parent
``libs/mngr_ovh/conftest.py``.
"""

from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_ovh.backend import OvhProvider
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.config import OvhProviderConfig


@pytest.fixture
def ovh_provider_factory(tmp_path: Path) -> Iterator[Callable[..., OvhProvider]]:
    """Return a factory that builds an ``OvhProvider`` wired to ``ovh_client``.

    The returned provider is fully constructed against a temp profile dir
    and a no-op plugin manager. Tests that drive ``_provision_vps`` should
    seed the SSH key they pass as ``vps_ssh_key_id`` onto ``ovh_client``
    themselves (via ``ovh_client.upload_ssh_key(...)``, an in-memory shim).
    The active ``ConcurrencyGroup`` lives for the duration of the test.
    """
    concurrency_group = ConcurrencyGroup(name="ovh-provider-test")
    with concurrency_group:

        def _make(
            ovh_client: OvhVpsClient,
            *,
            provider_name: str = "alice-ovh",
            enable_recycle_cancelled: bool = True,
            vps_boot_timeout: float = 600.0,
        ) -> OvhProvider:
            host_dir = tmp_path / ".mngr"
            host_dir.mkdir(exist_ok=True)
            profile_dir = host_dir / "profiles" / uuid4().hex
            profile_dir.mkdir(parents=True, exist_ok=True)
            config = MngrConfig(
                default_host_dir=host_dir,
                prefix="ovhtest",
                is_error_reporting_enabled=False,
            )
            mngr_ctx = make_mngr_ctx(
                config,
                pluggy.PluginManager("mngr"),
                profile_dir,
                concurrency_group=concurrency_group,
            )
            ovh_config = OvhProviderConfig(
                host_dir=host_dir,
                enable_recycle_cancelled=enable_recycle_cancelled,
                vps_boot_timeout=vps_boot_timeout,
            )
            return OvhProvider(
                name=ProviderInstanceName(provider_name),
                host_dir=host_dir,
                mngr_ctx=mngr_ctx,
                config=ovh_config,
                vps_client=ovh_client,
                ovh_client=ovh_client,
                ovh_config=ovh_config,
            )

        yield _make
