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
import pluggy
import pytest
from click.testing import CliRunner
from google.auth.credentials import AnonymousCredentials
from google.cloud import compute_v1

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr_gcp.backend import GCP_BACKEND_NAME
from imbue.mngr_gcp.cli import _perform_cleanup
from imbue.mngr_gcp.cli import _resolve_provider_config
from imbue.mngr_gcp.cli import gcp_cli_group
from imbue.mngr_gcp.config import GcpProviderConfig
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
    assert "--provider" in result.output
    assert "--project" in result.output
    assert "--allowed-ssh-cidr" in result.output


def test_cleanup_command_help_is_reachable() -> None:
    """`mngr gcp cleanup --help` should render without invoking GCP."""
    runner = CliRunner()
    result = runner.invoke(gcp_cli_group, ["cleanup", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--project" in result.output
    assert "--firewall-name" in result.output


def test_prepare_command_fails_clearly_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """When ADC isn't resolvable, the click command surfaces a clean error.

    Forcing no-ADC: point GOOGLE_APPLICATION_CREDENTIALS at a nonexistent file.
    ``google.auth.default()`` checks that env var first and raises
    ``DefaultCredentialsError`` immediately when it names a missing file, so the
    well-known ADC file is never consulted and the test is hermetic regardless of
    the host's gcloud state. Passes ``obj=plugin_manager`` because ``prepare`` now
    runs through ``setup_command_context`` (so it can read ``[providers.NAME]``
    from settings.toml as defaults), and ``setup_command_context`` reads the
    plugin manager off ``ctx.obj``.
    """
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/adc.json")
    result = cli_runner.invoke(
        gcp_cli_group,
        ["prepare", "--project", "test-project", "--allowed-ssh-cidr", "0.0.0.0/0"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "application default credentials not configured" in result.output.lower()


# =============================================================================
# Provider-config resolution (the bug-fix surface for `mngr gcp prepare`)
# =============================================================================
#
# Earlier versions of ``_build_operator_client`` used ``GcpProviderConfig()``
# class defaults unconditionally, so a user with a non-default ``default_zone``
# / ``network`` / ``firewall_name`` in ``[providers.gcp]`` running
# ``mngr gcp prepare`` without the matching CLI flag would land the firewall
# rule using class defaults while the runtime create path used whatever their
# settings.toml specified. ``_resolve_provider_config`` fixes this by reading
# the user's resolved provider config off the ``MngrContext``; these tests pin
# that behavior. Mirrors the AWS provider's identical fix.


def _temp_mngr_ctx_with_provider(temp_mngr_ctx: MngrContext, name: str, config: ProviderInstanceConfig) -> MngrContext:
    """Return ``temp_mngr_ctx`` with ``config`` registered under ``name`` in ``providers``."""
    provider_name = ProviderInstanceName(name)
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, {provider_name: config})
    )
    return temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))


def test_resolve_provider_config_uses_user_provider_block(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """The happy path returns the configured ``GcpProviderConfig`` verbatim, silently.

    Pins the third leg of the three-case contract: configured GCP block ->
    return as-is, no warning. The two sibling tests cover the missing-block and
    non-GCP-block fallbacks (silent and warning respectively); pinning silence
    here too closes the {GCP / non-GCP / missing} x {warn / silent} matrix so a
    future regression that always-warns can't slip through.
    """
    user_config = GcpProviderConfig(
        backend=GCP_BACKEND_NAME,
        project_id="my-project",
        default_region="europe-west1",
        default_zone="europe-west1-b",
        network="custom-net",
        firewall_name="my-fw",
    )
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "gcp-prod", user_config)

    resolved = _resolve_provider_config(ctx_with_provider, "gcp-prod")

    assert resolved.project_id == "my-project"
    assert resolved.default_zone == "europe-west1-b"
    assert resolved.network == "custom-net"
    assert resolved.firewall_name == "my-fw"
    assert log_warnings == [], f"happy path must be silent, got {log_warnings!r}"


def test_resolve_provider_config_falls_back_to_class_defaults_when_missing(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """When the named provider block doesn't exist, class defaults are used silently.

    Operator commands must work for first-run users who haven't yet pinned a
    ``[providers.gcp]`` block, so the fallback is a feature not a bug -- and no
    warning is emitted because this is the expected shape (distinct from the
    wrong-type case, which does warn).
    """
    resolved = _resolve_provider_config(temp_mngr_ctx, "gcp-does-not-exist")

    assert resolved == GcpProviderConfig()
    assert log_warnings == [], f"missing-block fallback must be silent, got {log_warnings!r}"


def test_resolve_provider_config_falls_back_when_named_block_is_non_gcp(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """If the user pointed ``[providers.gcp]`` at a non-GCP backend, fall back and warn.

    The operator CLI still works against the class defaults plus whatever the
    user passes on the command line; refusing here would block a legitimate
    out-of-band run. But the user's ``--provider`` selection did not have the
    intended effect, so a warning is emitted to make the silent-fallback visible
    (distinct from the missing-block case, which is silent because it is the
    expected first-run shape).
    """
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "gcp", LocalProviderConfig())

    resolved = _resolve_provider_config(ctx_with_provider, "gcp")

    assert resolved == GcpProviderConfig()
    assert len(log_warnings) == 1, f"expected exactly one warning, got {log_warnings!r}"
    assert "'gcp'" in log_warnings[0]
    assert "LocalProviderConfig" in log_warnings[0]
