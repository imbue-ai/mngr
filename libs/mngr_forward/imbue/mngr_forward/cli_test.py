"""Tests for ``mngr forward``'s CLI option validation.

These tests stub out heavy dependencies (the FastAPI app + uvicorn loop)
by inspecting only the option-validation phase via direct calls to the
helpers. End-to-end CLI invocation is exercised by the acceptance test.
"""

import click
import pytest

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.cli import ForwardCliOptions
from imbue.mngr_forward.cli import _build_strategy
from imbue.mngr_forward.cli import _filter_snapshot
from imbue.mngr_forward.cli import _parse_reverse_specs
from imbue.mngr_forward.cli import _validate_options
from imbue.mngr_forward.data_types import ForwardAgentSnapshot
from imbue.mngr_forward.data_types import ForwardListSnapshot
from imbue.mngr_forward.data_types import ForwardPortStrategy
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2


def _opts(**overrides: object) -> ForwardCliOptions:
    return ForwardCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
        **overrides,  # type: ignore[arg-type]
    )


def test_validation_requires_one_target() -> None:
    with pytest.raises(click.UsageError):
        _validate_options(_opts())


def test_validation_rejects_both_targets() -> None:
    with pytest.raises(click.UsageError):
        _validate_options(_opts(service="system_interface", forward_port=8080))


def test_validation_rejects_no_observe_with_service() -> None:
    with pytest.raises(click.UsageError):
        _validate_options(_opts(service="system_interface", no_observe=True))


def test_validation_accepts_no_observe_with_forward_port() -> None:
    _validate_options(_opts(forward_port=8080, no_observe=True))


def test_build_strategy_service() -> None:
    strategy = _build_strategy(_opts(service="system_interface"))
    assert isinstance(strategy, ForwardServiceStrategy)
    assert strategy.service_name == "system_interface"


def test_build_strategy_port() -> None:
    strategy = _build_strategy(_opts(forward_port=8080))
    assert isinstance(strategy, ForwardPortStrategy)
    assert strategy.remote_port == 8080


def test_parse_reverse_specs_dynamic_remote() -> None:
    specs = _parse_reverse_specs(("0:8420",))
    assert specs == (ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420)),)


def test_parse_reverse_specs_fixed_remote() -> None:
    specs = _parse_reverse_specs(("1989:7777",))
    assert specs == (ReverseTunnelSpec(remote_port=NonNegativeInt(1989), local_port=PositiveInt(7777)),)


def test_parse_reverse_specs_repeated() -> None:
    specs = _parse_reverse_specs(("8420:8420", "9090:9090"))
    assert len(specs) == 2
    assert specs[0].local_port == 8420
    assert specs[1].local_port == 9090


def test_parse_reverse_specs_rejects_missing_colon() -> None:
    with pytest.raises(click.UsageError):
        _parse_reverse_specs(("8420",))


def test_parse_reverse_specs_rejects_zero_local() -> None:
    with pytest.raises(click.UsageError):
        _parse_reverse_specs(("8420:0",))


def test_parse_reverse_specs_rejects_negative() -> None:
    with pytest.raises(click.UsageError):
        _parse_reverse_specs(("-1:8420",))


def test_parse_reverse_specs_rejects_non_integer() -> None:
    with pytest.raises(click.UsageError):
        _parse_reverse_specs(("abc:8420",))


def test_filter_snapshot_supports_provider_name_filter() -> None:
    """`--agent-include` / `--agent-exclude` must work the same in --no-observe mode

    as they do in observe mode, so a CEL expression referencing
    ``agent.provider_name`` (which observe mode populates) must also be
    available against the snapshot.
    """
    snapshot = ForwardListSnapshot(
        agents=(
            ForwardAgentSnapshot(agent_id=TEST_AGENT_ID_1, provider_name="modal"),
            ForwardAgentSnapshot(agent_id=TEST_AGENT_ID_2, provider_name="docker"),
        )
    )
    filtered = _filter_snapshot(snapshot, include=("agent.provider_name == 'modal'",), exclude=())
    assert tuple(entry.agent_id for entry in filtered.agents) == (TEST_AGENT_ID_1,)


def test_filter_snapshot_supports_host_id_and_name_filter() -> None:
    """All four observe-mode CEL fields are available against the snapshot."""
    snapshot = ForwardListSnapshot(
        agents=(
            ForwardAgentSnapshot(agent_id=TEST_AGENT_ID_1, host_id="host-a", agent_name="alpha"),
            ForwardAgentSnapshot(agent_id=TEST_AGENT_ID_2, host_id="host-b", agent_name="beta"),
        )
    )
    by_host = _filter_snapshot(snapshot, include=("agent.host_id == 'host-a'",), exclude=())
    assert tuple(entry.agent_id for entry in by_host.agents) == (TEST_AGENT_ID_1,)
    by_name = _filter_snapshot(snapshot, include=(), exclude=("agent.name == 'alpha'",))
    assert tuple(entry.agent_id for entry in by_name.agents) == (TEST_AGENT_ID_2,)
