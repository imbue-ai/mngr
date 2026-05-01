from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Annotated

import pytest
from pydantic import Field as PydanticField
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import OldestCreatedNoInputsError
from imbue.mngr_kanpan.data_source import StringField
from imbue.mngr_kanpan.data_source import deserialize_fields
from imbue.mngr_kanpan.data_source import now_utc
from imbue.mngr_kanpan.data_source import oldest_created
from imbue.mngr_kanpan.data_sources.git_info import CommitsAheadField
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import ConflictsField
from imbue.mngr_kanpan.data_sources.github import CreatePrUrlField
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_sources.github import UnresolvedField
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathField
from imbue.mngr_kanpan.testing import TEST_NOW

# === CellDisplay ===


def test_cell_display_defaults() -> None:
    cell = CellDisplay(text="hello")
    assert cell.text == "hello"
    assert cell.url is None
    assert cell.color is None


# === FieldValue ===


def test_field_value_base_display() -> None:
    fv = FieldValue(created=TEST_NOW)
    cell = fv.display()
    assert isinstance(cell, CellDisplay)


def test_field_value_requires_created() -> None:
    """Constructing a FieldValue subclass without `created` must raise.

    The `created` field is required (no default) precisely so that forgetting
    to propagate it from cached inputs surfaces as a ValidationError instead
    of a silent fresh-staleness mis-tag.
    """
    # Use model_validate to bypass the static type checker -- the runtime
    # validation is what we are exercising here, not the call signature.
    with pytest.raises(ValidationError):
        BoolField.model_validate({"value": True})


def test_field_value_preserves_explicit_created() -> None:
    explicit = datetime(2020, 1, 1, tzinfo=timezone.utc)
    field = StringField(value="x", created=explicit)
    assert field.created == explicit


def test_field_value_round_trip_preserves_created() -> None:
    field = PrField(
        number=1,
        url="https://example.com/1",
        is_draft=False,
        title="x",
        state=PrState.OPEN,
        head_branch="b",
        created=TEST_NOW,
    )
    dumped = field.model_dump()
    restored = PrField.model_validate(dumped)
    assert restored == field
    assert restored.created == TEST_NOW


# === oldest_created / now_utc ===


def test_now_utc_is_timezone_aware() -> None:
    now = now_utc()
    assert now.tzinfo is not None
    assert now.tzinfo.utcoffset(now) == timedelta(0)


def test_oldest_created_returns_min() -> None:
    older = datetime(2020, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = StringField(value="a", created=newer)
    b = StringField(value="b", created=older)
    assert oldest_created(a, b) == older


def test_oldest_created_skips_none_inputs() -> None:
    only = datetime(2020, 1, 1, tzinfo=timezone.utc)
    a = StringField(value="a", created=only)
    assert oldest_created(None, a, None) == only


def test_oldest_created_raises_on_all_none() -> None:
    with pytest.raises(OldestCreatedNoInputsError):
        oldest_created(None, None)


# === StringField ===


def test_string_field_display() -> None:
    field = StringField(value="test-value", created=TEST_NOW)
    cell = field.display()
    assert cell.text == "test-value"
    assert cell.url is None


# === BoolField ===


def test_bool_field_display_true() -> None:
    field = BoolField(value=True, created=TEST_NOW)
    assert field.display().text == "yes"


def test_bool_field_display_false() -> None:
    field = BoolField(value=False, created=TEST_NOW)
    assert field.display().text == "no"


# === PrField ===


def test_pr_field_display() -> None:
    pr = PrField(
        number=42,
        url="https://github.com/org/repo/pull/42",
        is_draft=False,
        title="Test PR",
        state=PrState.OPEN,
        head_branch="test-branch",
        created=TEST_NOW,
    )
    cell = pr.display()
    assert cell.text == "#42"
    assert cell.url == "https://github.com/org/repo/pull/42"


# === CiField ===


def test_ci_field_display_passing() -> None:
    cell = CiField(status=CiStatus.PASSING, created=TEST_NOW).display()
    assert cell.text == "passing"
    assert cell.color == "light green"


def test_ci_field_display_failing() -> None:
    cell = CiField(status=CiStatus.FAILING, created=TEST_NOW).display()
    assert cell.text == "failing"
    assert cell.color == "light red"


def test_ci_field_display_pending() -> None:
    cell = CiField(status=CiStatus.PENDING, created=TEST_NOW).display()
    assert cell.text == "pending"
    assert cell.color == "yellow"


def test_ci_field_display_unknown() -> None:
    cell = CiField(status=CiStatus.UNKNOWN, created=TEST_NOW).display()
    assert cell.text == ""
    assert cell.color is None


# === CreatePrUrlField ===


def test_create_pr_url_field_display() -> None:
    field = CreatePrUrlField(
        url="https://github.com/org/repo/compare/branch?expand=1",
        created=TEST_NOW,
    )
    cell = field.display()
    assert cell.text == "+PR"
    assert cell.url == "https://github.com/org/repo/compare/branch?expand=1"


# === RepoPathField ===


def test_repo_path_field_display() -> None:
    field = RepoPathField(path="org/repo", created=TEST_NOW)
    cell = field.display()
    assert cell.text == "org/repo"


# === CommitsAheadField ===


def test_commits_ahead_field_no_work_dir() -> None:
    field = CommitsAheadField(count=None, has_work_dir=False, created=TEST_NOW)
    assert field.display().text == ""


def test_commits_ahead_field_not_pushed() -> None:
    field = CommitsAheadField(count=None, has_work_dir=True, created=TEST_NOW)
    assert field.display().text == "[not pushed]"


def test_commits_ahead_field_up_to_date() -> None:
    field = CommitsAheadField(count=0, has_work_dir=True, created=TEST_NOW)
    assert field.display().text == "[up to date]"


def test_commits_ahead_field_has_unpushed() -> None:
    field = CommitsAheadField(count=3, has_work_dir=True, created=TEST_NOW)
    assert field.display().text == "[3 unpushed]"


# === ConflictsField ===


def test_conflicts_field_display_has_conflicts() -> None:
    cell = ConflictsField(has_conflicts=True, created=TEST_NOW).display()
    assert cell.text == "YES"
    assert cell.color == "light red"


def test_conflicts_field_display_no_conflicts() -> None:
    cell = ConflictsField(has_conflicts=False, created=TEST_NOW).display()
    assert cell.text == "no"
    assert cell.color == "light green"


# === UnresolvedField ===


def test_unresolved_field_display_has_unresolved() -> None:
    cell = UnresolvedField(has_unresolved=True, created=TEST_NOW).display()
    assert cell.text == "YES"
    assert cell.color == "light red"


def test_unresolved_field_display_no_unresolved() -> None:
    cell = UnresolvedField(has_unresolved=False, created=TEST_NOW).display()
    assert cell.text == "no"
    assert cell.color == "light green"


# === deserialize_fields ===


def test_deserialize_fields_basic() -> None:
    raw = {
        "pr": {
            "kind": "pr",
            "number": 42,
            "url": "https://example.com/42",
            "is_draft": False,
            "title": "Test",
            "state": "OPEN",
            "head_branch": "b",
            "created": TEST_NOW.isoformat(),
        },
        "ci": {"kind": "ci", "status": "FAILING", "created": TEST_NOW.isoformat()},
    }
    types: dict[str, TypeAdapter[FieldValue]] = {"pr": TypeAdapter(PrField), "ci": TypeAdapter(CiField)}
    result = deserialize_fields(raw, types)
    assert isinstance(result["pr"], PrField)
    assert result["pr"].number == 42
    assert isinstance(result["ci"], CiField)
    assert result["ci"].status == CiStatus.FAILING


def test_deserialize_fields_unknown_keys_skipped() -> None:
    raw = {"unknown_key": {"value": "test"}}
    result = deserialize_fields(raw, {"pr": TypeAdapter(PrField)})
    assert result == {}


def test_deserialize_fields_round_trip() -> None:
    pr = PrField(
        number=1,
        url="https://example.com/1",
        is_draft=True,
        title="Draft",
        state=PrState.OPEN,
        head_branch="branch",
        created=TEST_NOW,
    )
    dumped = {"pr": pr.model_dump(mode="json")}
    restored = deserialize_fields(dumped, {"pr": TypeAdapter(PrField)})
    assert restored["pr"] == pr


def test_deserialize_fields_polymorphic_via_discriminator() -> None:
    """A polymorphic slot is declared as a TypeAdapter wrapping a discriminated
    union. Pydantic dispatches on the ``kind`` tag to pick the right class, so
    the same slot accepts both PrField and CreatePrUrlField payloads.
    """
    pr_slot: TypeAdapter[FieldValue] = TypeAdapter(
        Annotated[PrField | CreatePrUrlField, PydanticField(discriminator="kind")]
    )
    pr_dump = PrField(
        number=7,
        url="https://example.com/7",
        is_draft=False,
        title="t",
        state=PrState.OPEN,
        head_branch="b",
        created=TEST_NOW,
    ).model_dump(mode="json")
    create_dump = CreatePrUrlField(url="https://example.com/compare", created=TEST_NOW).model_dump(mode="json")

    pr_result = deserialize_fields({"pr": pr_dump}, {"pr": pr_slot})
    create_result = deserialize_fields({"pr": create_dump}, {"pr": pr_slot})

    assert isinstance(pr_result["pr"], PrField)
    assert pr_result["pr"].number == 7
    assert isinstance(create_result["pr"], CreatePrUrlField)
    assert create_result["pr"].url == "https://example.com/compare"


def test_deserialize_fields_drops_invalid_payload_keeps_others() -> None:
    """A payload that fails pydantic validation is logged and dropped, while
    the rest of the dict still loads. Locks in the swallow path so a future
    change can't quietly turn a bad cache row into a full-cache wipe.
    """
    types: dict[str, TypeAdapter[FieldValue]] = {"pr": TypeAdapter(PrField), "ci": TypeAdapter(CiField)}
    # PrField requires number/url/title/state/head_branch/is_draft/created -- {} fails.
    raw = {"pr": {}, "ci": {"kind": "ci", "status": "PASSING", "created": TEST_NOW.isoformat()}}
    result = deserialize_fields(raw, types)
    assert "pr" not in result
    assert isinstance(result["ci"], CiField)
    assert result["ci"].status == CiStatus.PASSING
