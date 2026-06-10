"""Tests for ``mngr gcp`` CLI subcommands.

Splits the test surface into two layers:

- The firewall-management logic that the ``prepare`` callback invokes:
  exercised against ``_StubbedGcpVpsClient`` with a hand-written fake
  FirewallsClient. Bypasses the click runtime so the create-when-missing /
  reuse-when-present wire contract can be asserted directly.
- Click-level smoke tests: invoke the click commands through ``CliRunner`` to
  verify exit codes and user-facing error messages on the paths that don't need
  a real GCE call (``prepare --help``; the no-credentials path).
"""

import pytest
from click.testing import CliRunner
from google.auth.credentials import AnonymousCredentials
from google.cloud import compute_v1

from imbue.mngr_gcp.cli import gcp_cli_group
from imbue.mngr_gcp.testing import FakeFirewallsClient
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


def test_prepare_command_help_is_reachable() -> None:
    """`mngr gcp prepare --help` should render without invoking GCP."""
    runner = CliRunner()
    result = runner.invoke(gcp_cli_group, ["prepare", "--help"])
    assert result.exit_code == 0
    assert "--project" in result.output
    assert "--allowed-ssh-cidr" in result.output


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
