"""Unit tests for the OVH pool-host cleanup runbook helpers."""

import importlib.util
from pathlib import Path
from types import ModuleType

from imbue.remote_service_connector.app import OVH_PROVIDER_TAG_KEY
from imbue.remote_service_connector.app import OvhVpsResource
from imbue.remote_service_connector.app import vps_urn_for
from imbue.remote_service_connector.testing import FakeOvhOps

_RUNBOOK_PATH = Path(__file__).resolve().parent / "cleanup_released_hosts.py"


def _load_runbook() -> ModuleType:
    """Load the runbook script as a module (the ``scripts/`` dir is not a package)."""
    spec = importlib.util.spec_from_file_location("cleanup_released_hosts_runbook", _RUNBOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_strip_all_non_provider_tags_keeps_only_provider_tag() -> None:
    """Every tag except ``mngr-provider`` is stripped, and the provider tag is preserved."""
    runbook = _load_runbook()
    ovh_ops = FakeOvhOps()
    resource = OvhVpsResource(
        urn="urn:v1:us:resource:vps:vps-x.vps.ovh.us",
        name="vps-x.vps.ovh.us",
        tags={OVH_PROVIDER_TAG_KEY: "ovh", "minds_env": "dev", "mngr-host-id": "host-1"},
    )

    stripped = runbook._strip_all_non_provider_tags(ovh_ops, resource, "us")

    assert sorted(stripped) == ["minds_env", "mngr-host-id"]
    deleted_keys = {key for _urn, key in ovh_ops.deleted_tags}
    assert OVH_PROVIDER_TAG_KEY not in deleted_keys
    assert deleted_keys == {"minds_env", "mngr-host-id"}
    assert all(urn == resource.urn for urn, _key in ovh_ops.deleted_tags)


def test_strip_all_non_provider_tags_falls_back_to_built_urn() -> None:
    """When a resource has no URN, the helper builds one from the service name."""
    runbook = _load_runbook()
    ovh_ops = FakeOvhOps()
    resource = OvhVpsResource(urn="", name="vps-y.vps.ovh.us", tags={"minds_env": "dev"})

    runbook._strip_all_non_provider_tags(ovh_ops, resource, "us")

    assert ovh_ops.deleted_tags == [(vps_urn_for("vps-y.vps.ovh.us", "us"), "minds_env")]
