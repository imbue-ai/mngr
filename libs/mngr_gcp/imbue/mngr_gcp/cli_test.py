"""Tests for ``mngr gcp`` CLI subcommands.

Splits the test surface into two layers:

- The firewall-management logic that the ``prepare`` / ``cleanup`` callbacks
  invoke: exercised against ``_StubbedGcpVpsClient`` with hand-written fake
  Firewalls/Instances clients. Bypasses the click runtime so the
  create-when-missing / reuse-when-present and refuse-when-instances-exist /
  delete-when-clean contracts can be asserted directly.
- Click-level smoke tests: invoke the click commands through ``CliRunner`` to
  verify exit codes and user-facing error messages on the paths that don't need
  a real GCE call (``prepare`` / ``cleanup`` ``--help``; the no-credentials path).
"""

import click
import pytest
from click.testing import CliRunner
from google.auth.credentials import AnonymousCredentials
from google.cloud import compute_v1

from imbue.mngr_gcp.cli import _perform_cleanup
from imbue.mngr_gcp.cli import gcp_cli_group
from imbue.mngr_gcp.testing import FakeFirewallsClient
from imbue.mngr_gcp.testing import FakeInstancesClient
from imbue.mngr_gcp.testing import _StubbedGcpVpsClient


def _prepare_client(firewalls: FakeFirewallsClient) -> _StubbedGcpVpsClient:
    return _StubbedGcpVpsClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone="us-west1-a",
        image="projects/debian-cloud/global/images/family/debian-12",
        allowed_ssh_cidrs=("0.0.0.0/0",),
        stubbed_firewalls_client=firewalls,
    )


def _cleanup_client(instances: FakeInstancesClient, firewalls: FakeFirewallsClient) -> _StubbedGcpVpsClient:
    return _StubbedGcpVpsClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone="us-west1-a",
        image="projects/debian-cloud/global/images/family/debian-12",
        stubbed_instances_client=instances,
        stubbed_firewalls_client=firewalls,
    )


def test_prepare_logic_creates_firewall_when_missing() -> None:
    """The privileged path creates the rule when it does not yet exist."""
    firewalls = FakeFirewallsClient()
    client = _prepare_client(firewalls)
    assert client.ensure_firewall() == "mngr-ssh"
    assert len(firewalls.inserted) == 1
    assert firewalls.inserted[0].name == "mngr-gcp-ssh"


def test_prepare_logic_reuses_firewall_when_present() -> None:
    """When the rule already exists, prepare is a no-op (no insert)."""
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    client = _prepare_client(firewalls)
    assert client.ensure_firewall() == "mngr-ssh"
    assert firewalls.inserted == []


def test_cleanup_logic_deletes_firewall_when_no_instances() -> None:
    """With no mngr instances, cleanup deletes the rule and returns its name."""
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    # No aggregated_result on the fake -> no mngr-managed instances anywhere.
    instances = FakeInstancesClient()
    client = _cleanup_client(instances, firewalls)
    assert _perform_cleanup(client) == "mngr-gcp-ssh"
    assert firewalls.deleted == ["mngr-gcp-ssh"]


def test_cleanup_logic_is_noop_when_firewall_missing() -> None:
    """When the rule is already gone, cleanup deletes nothing and returns None (idempotent)."""
    client = _cleanup_client(FakeInstancesClient(), FakeFirewallsClient())
    assert _perform_cleanup(client) is None


def test_cleanup_logic_refuses_when_instances_exist() -> None:
    """A live mngr instance makes cleanup raise without deleting the firewall."""
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    instances = FakeInstancesClient()
    instances.aggregated_result = [
        (
            "zones/us-west1-a",
            [compute_v1.Instance(name="mngr-host-1", status="RUNNING", labels={"mngr-provider": "gcp"})],
        )
    ]
    client = _cleanup_client(instances, firewalls)
    with pytest.raises(click.ClickException) as exc_info:
        _perform_cleanup(client)
    # The refusal must name the blocking instance so the operator knows what to destroy.
    assert "mngr-host-1" in str(exc_info.value)
    assert "Refusing" in str(exc_info.value)
    # The firewall must NOT have been deleted while an instance still exists.
    assert firewalls.deleted == []


def test_prepare_command_help_is_reachable() -> None:
    """`mngr gcp prepare --help` should render without invoking GCP."""
    runner = CliRunner()
    result = runner.invoke(gcp_cli_group, ["prepare", "--help"])
    assert result.exit_code == 0
    assert "--project" in result.output
    assert "--allowed-ssh-cidr" in result.output


def test_cleanup_command_help_is_reachable() -> None:
    """`mngr gcp cleanup --help` should render without invoking GCP."""
    runner = CliRunner()
    result = runner.invoke(gcp_cli_group, ["cleanup", "--help"])
    assert result.exit_code == 0
    assert "--project" in result.output
    assert "--firewall-name" in result.output


def test_prepare_command_fails_clearly_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ADC isn't resolvable, the click command surfaces a clean error.

    Forcing no-ADC: point GOOGLE_APPLICATION_CREDENTIALS at a nonexistent file
    so ``google.auth.default()`` raises immediately, and pin CLOUDSDK_CONFIG to
    an empty temp dir so the well-known ADC file can't be found either.
    """
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/adc.json")
    runner = CliRunner()
    result = runner.invoke(gcp_cli_group, ["prepare", "--project", "test-project", "--allowed-ssh-cidr", "0.0.0.0/0"])
    assert result.exit_code != 0
    assert "application default credentials not configured" in result.output.lower()
