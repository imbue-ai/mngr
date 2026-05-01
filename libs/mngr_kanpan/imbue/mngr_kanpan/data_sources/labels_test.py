from datetime import datetime
from datetime import timezone

from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_sources.labels import LabelColumnConfig
from imbue.mngr_kanpan.data_sources.labels import LabelsDataSource
from imbue.mngr_kanpan.data_sources.labels import _ColoredStringField
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_mngr_ctx


def test_labels_data_source_is_not_remote() -> None:
    ds = LabelsDataSource(
        field_key="priority",
        config=LabelColumnConfig(header="PRIORITY", label_key="priority"),
    )
    assert ds.is_remote is False


def test_labels_compute_agent_with_label() -> None:
    ds = LabelsDataSource(
        field_key="priority",
        config=LabelColumnConfig(header="PRIORITY", label_key="priority"),
    )
    agent = make_agent_details(name="agent-1", labels={"priority": "high"})
    fields, errors = ds.compute(
        agents=(agent,),
        cached_fields={},
        mngr_ctx=make_mngr_ctx(),
    )
    assert errors == []
    assert agent.name in fields
    field = fields[agent.name]["priority"]
    assert isinstance(field, _ColoredStringField)
    assert field.value == "high"
    assert field.color is None


def test_labels_compute_agent_without_label() -> None:
    ds = LabelsDataSource(
        field_key="priority",
        config=LabelColumnConfig(header="PRIORITY", label_key="priority"),
    )
    agent = make_agent_details(name="agent-1", labels={})
    fields, errors = ds.compute(
        agents=(agent,),
        cached_fields={},
        mngr_ctx=make_mngr_ctx(),
    )
    assert errors == []
    assert agent.name not in fields


def test_labels_compute_with_color_map() -> None:
    ds = LabelsDataSource(
        field_key="priority",
        config=LabelColumnConfig(
            header="PRIORITY",
            label_key="priority",
            colors={"high": "light red", "low": "light green"},
        ),
    )
    agent = make_agent_details(name="agent-1", labels={"priority": "high"})
    fields, errors = ds.compute(
        agents=(agent,),
        cached_fields={},
        mngr_ctx=make_mngr_ctx(),
    )
    assert errors == []
    field = fields[agent.name]["priority"]
    assert isinstance(field, _ColoredStringField)
    assert field.color == "light red"


def test_labels_compute_color_not_in_map() -> None:
    ds = LabelsDataSource(
        field_key="priority",
        config=LabelColumnConfig(
            header="PRIORITY",
            label_key="priority",
            colors={"high": "light red"},
        ),
    )
    agent = make_agent_details(name="agent-1", labels={"priority": "medium"})
    fields, errors = ds.compute(
        agents=(agent,),
        cached_fields={},
        mngr_ctx=make_mngr_ctx(),
    )
    assert errors == []
    field = fields[agent.name]["priority"]
    assert isinstance(field, _ColoredStringField)
    assert field.color is None


def test_labels_compute_label_key_differs_from_field_key() -> None:
    ds = LabelsDataSource(
        field_key="prio_col",
        config=LabelColumnConfig(header="PRIO", label_key="priority"),
    )
    agent = make_agent_details(name="agent-1", labels={"priority": "urgent"})
    fields, errors = ds.compute(
        agents=(agent,),
        cached_fields={},
        mngr_ctx=make_mngr_ctx(),
    )
    assert errors == []
    assert agent.name in fields
    field = fields[agent.name]["prio_col"]
    assert isinstance(field, _ColoredStringField)
    assert field.value == "urgent"


def test_colored_string_field_display() -> None:
    field = _ColoredStringField(
        value="urgent", color="light red", created=datetime(2029, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    )
    result = field.display()
    assert isinstance(result, CellDisplay)
    assert result.text == "urgent"
    assert result.color == "light red"


def test_colored_string_field_display_no_color() -> None:
    field = _ColoredStringField(value="normal", created=datetime(2029, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    result = field.display()
    assert result.text == "normal"
    assert result.color is None


def test_labels_compute_multiple_agents() -> None:
    ds = LabelsDataSource(
        field_key="status",
        config=LabelColumnConfig(header="STATUS", label_key="status"),
    )
    agent_a = make_agent_details(name="agent-a", labels={"status": "active"})
    agent_b = make_agent_details(name="agent-b", labels={})
    agent_c = make_agent_details(name="agent-c", labels={"status": "idle"})
    fields, errors = ds.compute(
        agents=(agent_a, agent_b, agent_c),
        cached_fields={},
        mngr_ctx=make_mngr_ctx(),
    )
    assert errors == []
    assert agent_a.name in fields
    assert agent_b.name not in fields
    assert agent_c.name in fields
    assert fields[agent_a.name]["status"].display().text == "active"
    assert fields[agent_c.name]["status"].display().text == "idle"
