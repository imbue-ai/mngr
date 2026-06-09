from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.mngr.api.list import agent_details_to_cel_context
from imbue.mngr.cli.field_catalog import FieldContext
from imbue.mngr.cli.field_catalog import _CEL_COMPUTED_KEYS
from imbue.mngr.cli.field_catalog import build_list_field_catalog
from imbue.mngr.cli.field_catalog import catalog_rows_as_dicts
from imbue.mngr.cli.field_catalog import render_catalog_help_markdown
from imbue.mngr.cli.list import _FIELD_ALIASES
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName


def _catalog_keys() -> set[str]:
    return {row.key for row in build_list_field_catalog()}


def test_catalog_is_derived_from_the_live_model_shape() -> None:
    """Agent and host model fields, including deeply nested ones, are present.

    These come from walking AgentDetails/HostDetails, so their presence proves
    the catalog tracks the real data shape rather than a hand-maintained list.
    """
    keys = _catalog_keys()
    assert {"name", "state", "labels"} <= keys
    assert {"host.name", "host.provider_name", "host.resource.cpu.count", "host.ssh.host"} <= keys


def test_computed_keys_match_what_cel_context_actually_emits() -> None:
    """``_CEL_COMPUTED_KEYS`` must equal the keys agent_details_to_cel_context synthesizes.

    This pins the hand-written computed-field rows to the live computation: if a
    new computed field is added to ``agent_details_to_cel_context`` without a
    catalog row (or vice versa), this fails. The sample agent sets the optional
    inputs (runtime, activity times) so every conditional computed field appears.
    """
    now = datetime.now(timezone.utc)
    host = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        ssh_activity_time=now,
    )
    agent = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("sample"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work"),
        initial_branch=None,
        create_time=now,
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        runtime_seconds=10.0,
        user_activity_time=now,
        agent_activity_time=now,
        host=host,
    )

    context = agent_details_to_cel_context(agent)
    model_dump = agent.model_dump(mode="json")

    top_level_extra = set(context) - set(model_dump)
    host_extra = set(context["host"]) - set(model_dump["host"])
    emitted = top_level_extra | {f"host.{key}" for key in host_extra}

    assert emitted == set(_CEL_COMPUTED_KEYS), (
        f"agent_details_to_cel_context emits {sorted(emitted)} beyond the model, "
        f"but the catalog declares {sorted(_CEL_COMPUTED_KEYS)}; keep them in sync."
    )


def test_computed_rows_exist_and_are_cel_only_except_provider_alias() -> None:
    """Every computed key has a catalog row, restricted to CEL contexts.

    ``host.provider`` is the exception: it is also a documented template alias,
    so it is available in all three contexts.
    """
    rows_by_key = {row.key: row for row in build_list_field_catalog()}
    for key in _CEL_COMPUTED_KEYS:
        assert key in rows_by_key, f"computed key {key} has no catalog row"
    assert set(rows_by_key["age"].contexts) == {FieldContext.FILTER, FieldContext.SORT}
    assert FieldContext.TEMPLATE in rows_by_key["host.provider"].contexts


def test_catalog_covers_every_field_alias() -> None:
    """Each alias in ``_FIELD_ALIASES`` is documented as a catalog row.

    Pins the alias rows to the real alias table so a new alias cannot be added
    without surfacing it in the field catalog / help.
    """
    assert set(_FIELD_ALIASES) <= _catalog_keys()


def test_rows_as_dicts_collapse_contexts_to_string() -> None:
    rows = catalog_rows_as_dicts()
    age_row = next(row for row in rows if row["key"] == "age")
    assert age_row["contexts"] == "filter, sort"
    name_row = next(row for row in rows if row["key"] == "name")
    assert name_row["contexts"] == "filter, sort, template"


def test_help_markdown_renders_each_section_and_field() -> None:
    markdown = render_catalog_help_markdown()
    assert "**Agent fields:**" in markdown
    assert "**Host fields:**" in markdown
    assert "**Computed and alias fields:**" in markdown
    assert "`host.resource.cpu.count`" in markdown
    assert "`age` (filter, sort)" in markdown
