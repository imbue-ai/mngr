from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.mngr.api.list import agent_details_to_cel_context
from imbue.mngr.cli.field_catalog import FieldContext
from imbue.mngr.cli.field_catalog import FieldSection
from imbue.mngr.cli.field_catalog import _CEL_SYNTHESIZED_KEYS
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


def test_top_level_host_container_is_a_host_field() -> None:
    """The bare ``host`` container row is grouped with the host fields, not the agent ones.

    Its key lacks the ``host.`` dotted prefix shared by its descendants, so it must
    be classified explicitly; otherwise it renders awkwardly at the end of the agent
    section just before its own children.
    """
    rows_by_key = {row.key: row for row in build_list_field_catalog()}
    assert rows_by_key["host"].section == FieldSection.HOST


def test_catalog_omits_the_model_discriminator_tag() -> None:
    """The ``resource_type`` discriminator (Literal['agent']) is not a referenceable field.

    It is a constant model serialization tag, so it must not appear in the
    user-facing field catalog (and therefore not in the schema view or docs).
    """
    assert "resource_type" not in _catalog_keys()


def test_synthesized_keys_match_what_cel_context_actually_emits() -> None:
    """``_CEL_SYNTHESIZED_KEYS`` must equal the keys agent_details_to_cel_context adds.

    This pins the hand-written computed/alias rows to the live computation: if a
    new key is synthesized onto the CEL context (or removed) without updating the
    catalog, this fails. The sample agent sets the optional inputs (runtime,
    activity times, a project label) so every conditional synthesized key appears.
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
        labels={"project": "mngr"},
        host=host,
    )

    context = agent_details_to_cel_context(agent)
    model_dump = agent.model_dump(mode="json")

    top_level_extra = set(context) - set(model_dump)
    host_extra = set(context["host"]) - set(model_dump["host"])
    emitted = top_level_extra | {f"host.{key}" for key in host_extra}

    assert emitted == set(_CEL_SYNTHESIZED_KEYS), (
        f"agent_details_to_cel_context emits {sorted(emitted)} beyond the model, "
        f"but the catalog declares {sorted(_CEL_SYNTHESIZED_KEYS)}; keep them in sync."
    )


def test_synthesized_rows_exist_and_computed_fields_are_cel_only() -> None:
    """Every synthesized key has a catalog row; only the computed fields are CEL-only.

    age/runtime/idle cannot be produced by the template path, so they are
    ``cel``-only. The aliases (host.provider, project) resolve in both contexts.
    """
    rows_by_key = {row.key: row for row in build_list_field_catalog()}
    for key in _CEL_SYNTHESIZED_KEYS:
        assert key in rows_by_key, f"synthesized key {key} has no catalog row"
    for computed_key in ("age", "runtime", "idle"):
        assert set(rows_by_key[computed_key].contexts) == {FieldContext.CEL}
    for alias_key in ("host.provider", "project"):
        assert FieldContext.TEMPLATE in rows_by_key[alias_key].contexts


def test_catalog_covers_every_field_alias() -> None:
    """Each alias in ``_FIELD_ALIASES`` is documented as a catalog row.

    Pins the alias rows to the real alias table so a new alias cannot be added
    without surfacing it in the field catalog / help.
    """
    assert set(_FIELD_ALIASES) <= _catalog_keys()


def test_no_orphan_computed_or_alias_rows() -> None:
    """Every row in the "Computed and alias fields" section is backed by reality.

    Each such row must be either a synthesized CEL key (``_CEL_SYNTHESIZED_KEYS``) or a
    real alias (``_FIELD_ALIASES``). This is the reverse of the two pins above and
    catches a row left behind in the catalog after its source is removed.
    """
    backed = set(_CEL_SYNTHESIZED_KEYS) | set(_FIELD_ALIASES)
    computed_rows = [row for row in build_list_field_catalog() if row.section == FieldSection.COMPUTED]
    orphans = [row.key for row in computed_rows if row.key not in backed]
    assert not orphans, f"catalog has computed/alias rows not backed by the computation or alias table: {orphans}"


def test_rows_as_dicts_collapse_contexts_to_string() -> None:
    rows = catalog_rows_as_dicts()
    age_row = next(row for row in rows if row["key"] == "age")
    assert age_row["contexts"] == "cel"
    name_row = next(row for row in rows if row["key"] == "name")
    assert name_row["contexts"] == "cel, template"


def test_help_markdown_renders_each_section_and_field() -> None:
    markdown = render_catalog_help_markdown()
    assert "**Agent fields:**" in markdown
    assert "**Host fields:**" in markdown
    assert "**Computed and alias fields:**" in markdown
    assert "`host.resource.cpu.count`" in markdown


def test_help_markdown_flags_only_cel_only_fields() -> None:
    """Template-capable fields are unmarked; only the cel-only computed fields are flagged."""
    markdown = render_catalog_help_markdown()
    # age/runtime/idle cannot be used in templates, so they carry the marker.
    assert "`age` `(cel only)`" in markdown
    # A template-capable field (and the alias `project`) must NOT carry the marker.
    assert "`name` `(cel only)`" not in markdown
    assert "`project` `(cel only)`" not in markdown
