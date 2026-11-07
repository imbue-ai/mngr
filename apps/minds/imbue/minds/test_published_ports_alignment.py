"""Verify the published ports written onto cloud connection blocks match the
ports default-workspace-template's cloud-VM create templates publish.

Every aws / gcp / azure connection instance block the materializer writes
carries ``firewall_published_ports = CLOUD_CONNECTION_FIREWALL_PUBLISHED_PORTS``:
each cloud's ``prepare`` opens those ports in the firewall it manages, and
mngr_vps refuses a create that publishes a port the block does not list. Imbue
Studio creates a cloud workspace with ``--template main --template <cloud>``,
so the ports it actually publishes are the ``publish`` declarations of that
stack. If the two drift, every create on such a connection fails with
``PublishedPortUnsupportedError`` before a machine exists.

This test reads both values and asserts they match, per cloud.
"""

import tomllib

import pytest

from imbue.minds.mngr_settings.provider_blocks import CLOUD_CONNECTION_FIREWALL_PUBLISHED_PORTS
from imbue.minds.testing import DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO
from imbue.minds.testing import fetch_default_workspace_template_file
from imbue.minds.testing import stacked_create_template_published_ports
from imbue.mngr.primitives import parse_published_port_declarations

_TEMPLATE_SETTINGS_PATH = ".mngr/settings.toml"
_BASE_CREATE_TEMPLATE_NAME = "main"
_CLOUD_VM_CREATE_TEMPLATE_NAMES = ("aws", "gcp", "azure")


@pytest.mark.release
def test_cloud_connection_firewall_ports_match_template_published_ports() -> None:
    settings_source = fetch_default_workspace_template_file(_TEMPLATE_SETTINGS_PATH)
    assert settings_source is not None, (
        f"Failed to fetch {DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO}:{_TEMPLATE_SETTINGS_PATH}. "
        "Check template repo reachability."
    )
    settings = tomllib.loads(settings_source)
    firewall_ports = set(parse_published_port_declarations(CLOUD_CONNECTION_FIREWALL_PUBLISHED_PORTS))

    for template_name in _CLOUD_VM_CREATE_TEMPLATE_NAMES:
        declared_ports = set(
            stacked_create_template_published_ports(settings, (_BASE_CREATE_TEMPLATE_NAME, template_name))
        )
        assert declared_ports == firewall_ports, (
            f"CLOUD_CONNECTION_FIREWALL_PUBLISHED_PORTS (mngr_settings/provider_blocks.py) has drifted from the "
            f"ports a `--template {_BASE_CREATE_TEMPLATE_NAME} --template {template_name}` create publishes in "
            f"{DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO}; "
            f"only in the app: {sorted(str(port) for port in firewall_ports - declared_ports)}, "
            f"only in the template: {sorted(str(port) for port in declared_ports - firewall_ports)}."
        )
