from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import StringField
from imbue.mngr_kanpan.data_source import deserialize_fields
from imbue.mngr_kanpan.data_sources.git_info import CommitsAheadField
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import ConflictsField
from imbue.mngr_kanpan.data_sources.github import CreatePrUrlField
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_sources.github import UnresolvedField
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathField

# === CellDisplay ===


def test_cell_display_defaults() -> None:
    cell = CellDisplay(text="hello")
    assert cell.text == "hello"
    assert cell.url is None
    assert cell.color is None


# === FieldValue.display ===


def test_field_value_base_display() -> None:
    fv = FieldValue()
    cell = fv.display()
    assert isinstance(cell, CellDisplay)


# === StringField ===


def test_string_field_display() -> None:
    field = StringField(value="test-value")
    cell = field.display()
    assert cell.text == "test-value"
    assert cell.url is None


# === BoolField ===


def test_bool_field_display_true() -> None:
    field = BoolField(value=True)
    assert field.display().text == "yes"


def test_bool_field_display_false() -> None:
    field = BoolField(value=False)
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
    )
    cell = pr.display()
    assert cell.text == "#42"
    assert cell.url == "https://github.com/org/repo/pull/42"


# === CiField ===


def test_ci_field_display_passing() -> None:
    cell = CiField(status=CiStatus.PASSING).display()
    assert cell.text == "passing"
    assert cell.color == "light green"


def test_ci_field_display_failing() -> None:
    cell = CiField(status=CiStatus.FAILING).display()
    assert cell.text == "failing"
    assert cell.color == "light red"


def test_ci_field_display_pending() -> None:
    cell = CiField(status=CiStatus.PENDING).display()
    assert cell.text == "pending"
    assert cell.color == "yellow"


def test_ci_field_display_unknown() -> None:
    cell = CiField(status=CiStatus.UNKNOWN).display()
    assert cell.text == ""
    assert cell.color is None


# === CreatePrUrlField ===


def test_create_pr_url_field_display() -> None:
    field = CreatePrUrlField(url="https://github.com/org/repo/compare/branch?expand=1")
    cell = field.display()
    assert cell.text == "+PR"
    assert cell.url == "https://github.com/org/repo/compare/branch?expand=1"


# === RepoPathField ===


def test_repo_path_field_display() -> None:
    field = RepoPathField(path="org/repo")
    cell = field.display()
    assert cell.text == "org/repo"


# === CommitsAheadField ===


def test_commits_ahead_field_no_work_dir() -> None:
    field = CommitsAheadField(count=None, has_work_dir=False)
    assert field.display().text == ""


def test_commits_ahead_field_not_pushed() -> None:
    field = CommitsAheadField(count=None, has_work_dir=True)
    assert field.display().text == "[not pushed]"


def test_commits_ahead_field_up_to_date() -> None:
    field = CommitsAheadField(count=0, has_work_dir=True)
    assert field.display().text == "[up to date]"


def test_commits_ahead_field_has_unpushed() -> None:
    field = CommitsAheadField(count=3, has_work_dir=True)
    assert field.display().text == "[3 unpushed]"


# === ConflictsField ===


def test_conflicts_field_display_has_conflicts() -> None:
    cell = ConflictsField(has_conflicts=True).display()
    assert cell.text == "YES"
    assert cell.color == "light red"


def test_conflicts_field_display_no_conflicts() -> None:
    cell = ConflictsField(has_conflicts=False).display()
    assert cell.text == "no"
    assert cell.color == "light green"


# === UnresolvedField ===


def test_unresolved_field_display_has_unresolved() -> None:
    cell = UnresolvedField(has_unresolved=True).display()
    assert cell.text == "YES"
    assert cell.color == "light red"


def test_unresolved_field_display_no_unresolved() -> None:
    cell = UnresolvedField(has_unresolved=False).display()
    assert cell.text == "no"
    assert cell.color == "light green"


# === deserialize_fields ===


def test_deserialize_fields_basic() -> None:
    raw = {
        "pr": {
            "number": 42,
            "url": "https://example.com/42",
            "is_draft": False,
            "title": "Test",
            "state": "OPEN",
            "head_branch": "b",
        },
        "ci": {"status": "FAILING"},
    }
    types: dict[str, type[FieldValue]] = {"pr": PrField, "ci": CiField}
    result = deserialize_fields(raw, types)
    assert isinstance(result["pr"], PrField)
    assert result["pr"].number == 42
    assert isinstance(result["ci"], CiField)
    assert result["ci"].status == CiStatus.FAILING


def test_deserialize_fields_unknown_keys_skipped() -> None:
    raw = {"unknown_key": {"value": "test"}}
    result = deserialize_fields(raw, {"pr": PrField})
    assert result == {}


def test_deserialize_fields_round_trip() -> None:
    pr = PrField(
        number=1,
        url="https://example.com/1",
        is_draft=True,
        title="Draft",
        state=PrState.OPEN,
        head_branch="branch",
    )
    dumped = {"pr": pr.model_dump()}
    restored = deserialize_fields(dumped, {"pr": PrField})
    assert restored["pr"] == pr
