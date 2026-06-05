"""Unit tests for the destroy CLI command."""

import json
from typing import cast

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.destroy import DestroyCliOptions
from imbue.mngr.cli.destroy import _DestroyTargets
from imbue.mngr.cli.destroy import _OfflineHostToDestroy
from imbue.mngr.cli.destroy import _destroy_emptied_hosts
from imbue.mngr.cli.destroy import _emit_dry_run_entries
from imbue.mngr.cli.destroy import _output_result
from imbue.mngr.cli.destroy import destroy
from imbue.mngr.cli.destroy import get_agent_name_from_session
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.local.instance import LocalProviderInstance


def test_get_agent_name_from_session_extracts_name() -> None:
    """Test that get_agent_name_from_session extracts the agent name correctly."""
    result = get_agent_name_from_session("mngr-my-agent", "mngr-")
    assert result == "my-agent"


def test_get_agent_name_from_session_returns_none_for_empty_session() -> None:
    """Test that get_agent_name_from_session returns None for empty session name."""
    result = get_agent_name_from_session("", "mngr-")
    assert result is None


def test_get_agent_name_from_session_returns_none_when_prefix_does_not_match() -> None:
    """Test that get_agent_name_from_session returns None when session doesn't match prefix."""
    result = get_agent_name_from_session("other-session-name", "mngr-")
    assert result is None


def test_get_agent_name_from_session_returns_none_when_agent_name_empty() -> None:
    """Test that get_agent_name_from_session returns None when agent name is empty after prefix."""
    result = get_agent_name_from_session("mngr-", "mngr-")
    assert result is None


def test_offline_host_to_destroy_holds_real_interface_objects(
    local_host: Host,
    local_provider: LocalProviderInstance,
) -> None:
    """_OfflineHostToDestroy must accept real interface instances in its
    interface-typed fields (this is the whole point of arbitrary_types_allowed:
    without it, pydantic would reject HostInterface/ProviderInstanceInterface).

    The local provider doesn't support true offline hosts, but ``local_host`` is
    itself a concrete ``HostInterface``, which is all the field's annotation (and
    arbitrary_types_allowed) requires.
    """
    agent_id = AgentId.generate()
    target = _OfflineHostToDestroy(
        host=local_host,
        provider=local_provider,
        agent_names=[AgentName("agent-a")],
        agent_ids=[agent_id],
    )
    assert target.host is local_host
    assert target.provider is local_provider
    assert target.agent_names == [AgentName("agent-a")]
    assert target.agent_ids == [agent_id]


def test_destroy_targets_holds_real_interface_objects(
    local_host: Host,
    local_provider: LocalProviderInstance,
) -> None:
    """_DestroyTargets must accept real interface instances (arbitrary_types_allowed)
    and default online_hosts_with_provider to an empty list when omitted.
    """
    offline = _OfflineHostToDestroy(
        host=local_host,
        provider=local_provider,
        agent_names=[AgentName("agent-a")],
        agent_ids=[AgentId.generate()],
    )
    targets = _DestroyTargets(online_agents=[], offline_hosts=[offline])
    assert targets.online_agents == []
    assert targets.offline_hosts == [offline]
    # online_hosts_with_provider has a default_factory of list.
    assert targets.online_hosts_with_provider == []


@pytest.mark.parametrize(
    "flag,attr",
    [
        ("--force", "force"),
        ("--remove-created-branch", "remove_created_branch"),
        ("--no-gc", "gc"),
        ("--no-allow-worktree-removal", "allow_worktree_removal"),
    ],
)
def test_destroy_click_flags_map_to_cli_option_fields(flag: str, attr: str) -> None:
    """Each destroy click flag must populate the matching DestroyCliOptions field.

    setup_command_context builds DestroyCliOptions from the click params by name,
    so a flag whose click ``dest`` drifts from the model field name would silently
    fail to populate. Assert the flag's option exists, targets the expected field,
    and that the field is declared on the model.
    """
    matching = [p for p in destroy.params if attr == p.name]
    assert len(matching) == 1, f"expected exactly one click param named {attr!r}"
    param = matching[0]
    assert flag in param.opts or flag in param.secondary_opts
    assert attr in DestroyCliOptions.model_fields


def test_destroy_requires_agent_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that destroy requires at least one agent."""
    result = cli_runner.invoke(
        destroy,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent" in result.output


def test_destroy_session_fails_with_invalid_prefix(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session fails when session doesn't match expected prefix format."""
    result = cli_runner.invoke(
        destroy,
        ["--session", "not-mngr-prefix"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "does not match the expected format" in result.output


def test_destroy_session_cannot_combine_with_agent_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with agent names."""
    result = cli_runner.invoke(
        destroy,
        ["my-agent", "--session", "mngr-some-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "Cannot specify --session with agent names" in result.output


# =============================================================================
# Output helper function tests
# =============================================================================


def test_destroy_output_result_human_with_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in HUMAN format with destroyed agents."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result([AgentName("agent-a"), AgentName("agent-b")], output_opts)
    captured = capsys.readouterr()
    assert "Successfully destroyed 2 agent(s)" in captured.out


def test_destroy_output_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSON format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _output_result([AgentName("agent-x")], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["destroyed_agents"] == ["agent-x"]
    assert data["count"] == 1


def test_destroy_output_result_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSONL format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _output_result([AgentName("agent-y")], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "destroy_result"
    assert data["count"] == 1


def test_destroy_output_result_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result with a format template."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{name}")
    _output_result([AgentName("my-agent")], output_opts)
    captured = capsys.readouterr()
    # The "{name}" template renders exactly the agent name, one line, with no
    # surrounding human-format decoration.
    assert captured.out.strip() == "my-agent"


# =============================================================================
# Agent address support in destroy
# =============================================================================


def test_destroy_accepts_address_syntax(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Destroy should parse agent addresses without crashing.

    When given NAME@HOST.PROVIDER, the address is parsed and the agent name
    is extracted for matching. The command fails because the agent doesn't exist,
    not because of a parsing error.
    """
    result = cli_runner.invoke(
        destroy,
        ["my-agent@somehost.docker"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    # Should report agent not found (address was parsed, name extracted for matching)
    assert "my-agent" in result.output


def test_destroy_address_force_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Destroy with --force should not crash when address doesn't match any agent."""
    result = cli_runner.invoke(
        destroy,
        ["nonexistent@host.modal", "--force"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    # --force swallows AgentNotFoundError and returns 0, but still reports the
    # not-found agent by name so the failure is visible (not silently dropped).
    assert result.exit_code == 0
    assert "Error destroying agent(s): Agent not found: No agent(s) found matching: nonexistent" in result.output


def test_destroy_plain_name_still_works(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Plain agent names (no @) continue to work with the address-aware destroy."""
    result = cli_runner.invoke(
        destroy,
        ["plain-agent-name", "--force"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    # --force swallows the not-found error, but the name is still reported.
    assert result.exit_code == 0
    assert "Error destroying agent(s): Agent not found: No agent(s) found matching: plain-agent-name" in result.output


# =============================================================================
# stdin '-' placeholder tests
# =============================================================================


def test_destroy_dash_reads_agent_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that '-' reads agent names from stdin and passes them as identifiers."""
    result = cli_runner.invoke(
        destroy,
        ["-", "--force"],
        input="agent-from-stdin\n",
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # --force swallows the not-found error, exits 0; the stdin-read name is
    # still reported so it isn't silently dropped.
    assert result.exit_code == 0
    assert "Error destroying agent(s): Agent not found: No agent(s) found matching: agent-from-stdin" in result.output


def test_destroy_dash_empty_input_is_noop(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that '-' with empty stdin is a no-op (not an error)."""
    result = cli_runner.invoke(
        destroy,
        ["-"],
        input="",
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # Empty stdin returns early before any destroy work -- no error output.
    assert result.exit_code == 0
    assert "Error destroying" not in result.output
    assert "No agent(s) found matching" not in result.output


def test_destroy_dash_multiple_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that '-' reads multiple agent names from stdin."""
    result = cli_runner.invoke(
        destroy,
        ["-", "--force"],
        input="agent-one\nagent-two\nagent-three\n",
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # --force swallows the not-found error; all three stdin names are reported
    # (sorted) in a single not-found message.
    assert result.exit_code == 0
    assert (
        "Error destroying agent(s): Agent not found: No agent(s) found matching: agent-one, agent-three, agent-two"
        in result.output
    )


def test_destroy_dash_strips_whitespace(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that '-' strips whitespace from names."""
    result = cli_runner.invoke(
        destroy,
        ["-", "--force"],
        input="  agent-padded  \n\n  \n",
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # Whitespace and blank lines are stripped: only the trimmed "agent-padded"
    # is parsed and reported as not-found (no padded variant).
    assert result.exit_code == 0
    assert "Error destroying agent(s): Agent not found: No agent(s) found matching: agent-padded" in result.output


# =============================================================================
# _destroy_emptied_hosts -- the "destroy host when its last agent was
# destroyed" post-loop sweep. Asserts the documented destroy-CLI contract
# fires immediately for young hosts (not deferred to gc_machines' min-age
# check, which is what was leaking imbue_cloud leases until the 7-day
# destroyed-host grace period expired).
# =============================================================================


class _StubOnlineHost:
    """Duck-typed stand-in for ``OnlineHostInterface``.

    Implements ``id``, ``get_name``, ``get_agents`` (what the sweep itself
    reads) plus ``discover_agents`` returning an empty list -- needed so
    plugin hooks like ``mngr_claude``'s ``on_before_host_destroy`` (which
    iterates ``host.discover_agents()``) short-circuit harmlessly. The
    sweep's own logic doesn't call ``discover_agents``.
    """

    def __init__(
        self,
        host_id: HostId | None = None,
        remaining_agents: list[object] | None = None,
        raise_on_get_agents: Exception | None = None,
    ) -> None:
        self.id = host_id if host_id is not None else HostId.generate()
        self._remaining_agents = list(remaining_agents) if remaining_agents else []
        self._raise_on_get_agents = raise_on_get_agents

    def get_name(self) -> HostName:
        return HostName(f"stub-host-{self.id}")

    def get_agents(self) -> list[object]:
        if self._raise_on_get_agents is not None:
            raise self._raise_on_get_agents
        return list(self._remaining_agents)

    def discover_agents(self) -> list[object]:
        # Plugin-hook short-circuit: an empty discover list means the hook
        # has nothing to preserve / no sessions to save.
        return []


class _RecordingProvider:
    """Duck-typed stand-in for ``ProviderInstanceInterface`` that records destroy_host calls."""

    def __init__(
        self,
        raise_on_destroy: Exception | None = None,
    ) -> None:
        self.destroyed_hosts: list[object] = []
        self._raise_on_destroy = raise_on_destroy

    def destroy_host(self, host: object) -> None:
        if self._raise_on_destroy is not None:
            raise self._raise_on_destroy
        self.destroyed_hosts.append(host)


def _pair_for_emptied(
    host: _StubOnlineHost, provider: _RecordingProvider
) -> tuple[OnlineHostInterface, ProviderInstanceInterface]:
    return cast(OnlineHostInterface, host), cast(ProviderInstanceInterface, provider)


def test_destroy_emptied_hosts_destroys_host_when_no_agents_remain(temp_mngr_ctx: MngrContext) -> None:
    """A host whose last live agent was just destroyed gets destroyed by the post-loop sweep."""
    host = _StubOnlineHost(remaining_agents=[])
    provider = _RecordingProvider()

    _destroy_emptied_hosts(
        online_hosts_with_provider=[_pair_for_emptied(host, provider)],
        mngr_ctx=temp_mngr_ctx,
        output_opts=OutputOptions(output_format=OutputFormat.HUMAN),
    )

    assert provider.destroyed_hosts == [host], (
        "Empty online host must be destroyed by the post-loop sweep so cloud-side "
        "resources (lease / VPS / btrfs subvolume) don't leak until the 7-day "
        "destroyed-host grace period eventually triggers delete_host."
    )


def test_destroy_emptied_hosts_skips_host_with_remaining_agents(temp_mngr_ctx: MngrContext) -> None:
    """A host that still has live agents (e.g. only some targeted) is left alive."""
    # One live agent remains on the host -- destroy CLI must NOT take the host
    # out from under it.
    host = _StubOnlineHost(remaining_agents=[object()])
    provider = _RecordingProvider()

    _destroy_emptied_hosts(
        online_hosts_with_provider=[_pair_for_emptied(host, provider)],
        mngr_ctx=temp_mngr_ctx,
        output_opts=OutputOptions(output_format=OutputFormat.HUMAN),
    )

    assert provider.destroyed_hosts == [], (
        "Host with remaining live agents must NOT be destroyed -- the destroy CLI's "
        "'destroy host when last agent gone' contract only fires when the host is empty."
    )


@pytest.mark.allow_warnings(match="Cannot re-check host")
def test_destroy_emptied_hosts_skips_host_when_get_agents_raises_connection_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A transient connection failure during the re-check must not destroy the host.

    Cloud-side state is unknown if we can't talk to the host; the post-destroy
    GC pass (which has its own retry semantics) is the safety net.
    """
    host = _StubOnlineHost(
        raise_on_get_agents=HostConnectionError("Connection timed out"),
    )
    provider = _RecordingProvider()

    _destroy_emptied_hosts(
        online_hosts_with_provider=[_pair_for_emptied(host, provider)],
        mngr_ctx=temp_mngr_ctx,
        output_opts=OutputOptions(output_format=OutputFormat.HUMAN),
    )

    assert provider.destroyed_hosts == []


@pytest.mark.allow_warnings(match="Failed to destroy emptied host")
def test_destroy_emptied_hosts_tolerates_destroy_host_mngr_error(temp_mngr_ctx: MngrContext) -> None:
    """A provider.destroy_host failure on one host must not block others.

    Sweep processes hosts sequentially; a single broken host must surface as a
    warning (logged) and let the rest of the loop proceed. The post-destroy GC
    pass is the safety net for the failed one.
    """
    empty_host_a = _StubOnlineHost(remaining_agents=[])
    empty_host_b = _StubOnlineHost(remaining_agents=[])
    failing_provider = _RecordingProvider(raise_on_destroy=MngrError("provider broke"))
    working_provider = _RecordingProvider()

    _destroy_emptied_hosts(
        online_hosts_with_provider=[
            _pair_for_emptied(empty_host_a, failing_provider),
            _pair_for_emptied(empty_host_b, working_provider),
        ],
        mngr_ctx=temp_mngr_ctx,
        output_opts=OutputOptions(output_format=OutputFormat.HUMAN),
    )

    # The failing host's destroy was attempted (it raised before recording).
    # The working host's destroy still ran after the failure -- this is the
    # "one bad host doesn't block the others" guarantee.
    assert failing_provider.destroyed_hosts == []
    assert working_provider.destroyed_hosts == [empty_host_b]


def test_destroy_emptied_hosts_does_nothing_for_empty_input(temp_mngr_ctx: MngrContext) -> None:
    """No online hosts touched (e.g. all targets were offline) -> sweep is a no-op."""
    _destroy_emptied_hosts(
        online_hosts_with_provider=[],
        mngr_ctx=temp_mngr_ctx,
        output_opts=OutputOptions(output_format=OutputFormat.HUMAN),
    )
    # No assertions on side effects; we're just verifying it doesn't crash.


# =============================================================================
# --dry-run: preview targets without destroying anything.
# =============================================================================


def test_destroy_dry_run_output_human_lists_agents_and_marks_offline(capsys: pytest.CaptureFixture[str]) -> None:
    """Dry-run HUMAN output names each agent that would be destroyed and marks offline ones."""
    entries = [
        {"name": "agent-a", "host": "host-1", "offline": "false"},
        {"name": "agent-b", "host": "host-2", "offline": "true"},
    ]
    _emit_dry_run_entries(entries, OutputOptions(output_format=OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert "Would destroy 2 agent(s)" in captured.out
    assert "agent-a@host-1" in captured.out
    assert "agent-b@host-2 (offline)" in captured.out
    # An online agent must NOT be annotated as offline.
    assert "agent-a@host-1 (offline)" not in captured.out


def test_destroy_dry_run_output_json_reports_count(capsys: pytest.CaptureFixture[str]) -> None:
    """Dry-run JSON output is machine-readable and flags dry_run=True with a count."""
    entries = [{"name": "agent-x", "host": "host-1", "offline": "false"}]
    _emit_dry_run_entries(entries, OutputOptions(output_format=OutputFormat.JSON))
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["dry_run"] is True
    assert data["count"] == 1
    assert data["agents"][0]["name"] == "agent-x"


def test_destroy_dry_run_output_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """Dry-run honors a custom --format template, emitting one line per agent."""
    entries = [
        {"name": "agent-a", "host": "host-1", "offline": "false"},
        {"name": "agent-b", "host": "host-2", "offline": "true"},
    ]
    _emit_dry_run_entries(entries, OutputOptions(output_format=OutputFormat.HUMAN, format_template="{name}"))
    captured = capsys.readouterr()
    assert "agent-a" in captured.out
    assert "agent-b" in captured.out


def test_destroy_dry_run_empty_input_is_noop(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'-' with empty stdin plus --dry-run is a no-op (mirrors filtered pipeline with no matches)."""
    result = cli_runner.invoke(
        destroy,
        ["-", "--force", "--dry-run"],
        input="",
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code == 0
