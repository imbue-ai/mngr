import pytest

from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import CiField
from imbue.mngr_kanpan.data_source import CiStatus
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_MUTED
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanFieldTypeError
from imbue.mngr_kanpan.data_source import PrField
from imbue.mngr_kanpan.data_source import PrState
from imbue.mngr_kanpan.data_source import StringField
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.fetcher import _merge_cached_fields
from imbue.mngr_kanpan.fetcher import _parse_github_repo_path
from imbue.mngr_kanpan.fetcher import compute_section
from imbue.mngr_kanpan.fetcher import repo_path_from_labels

# === repo path parsing ===


def test_parse_ssh_url() -> None:
    assert _parse_github_repo_path("git@github.com:imbue-ai/mngr.git") == "imbue-ai/mngr"


def test_parse_ssh_url_without_git_suffix() -> None:
    assert _parse_github_repo_path("git@github.com:imbue-ai/mngr") == "imbue-ai/mngr"


def test_parse_https_url() -> None:
    assert _parse_github_repo_path("https://github.com/imbue-ai/mngr.git") == "imbue-ai/mngr"


def test_parse_https_url_without_git_suffix() -> None:
    assert _parse_github_repo_path("https://github.com/imbue-ai/mngr") == "imbue-ai/mngr"


def test_parse_non_github_url() -> None:
    assert _parse_github_repo_path("https://gitlab.com/org/repo.git") is None


def test_repo_path_from_labels_with_remote() -> None:
    assert repo_path_from_labels({"remote": "git@github.com:org/repo.git"}) == "org/repo"


def test_repo_path_from_labels_without_remote() -> None:
    assert repo_path_from_labels({}) is None


# === compute_section ===


def _make_pr(state: PrState = PrState.OPEN, is_draft: bool = False) -> PrField:
    return PrField(
        number=1,
        title="Test PR",
        state=state,
        url="https://github.com/org/repo/pull/1",
        head_branch="test-branch",
        is_draft=is_draft,
    )


def test_compute_section_muted() -> None:
    fields: dict[str, FieldValue] = {FIELD_MUTED: BoolField(value=True)}
    assert compute_section(fields) == BoardSection.MUTED


def test_compute_section_muted_false() -> None:
    fields: dict[str, FieldValue] = {FIELD_MUTED: BoolField(value=False)}
    assert compute_section(fields) == BoardSection.STILL_COOKING


def test_compute_section_no_pr() -> None:
    fields: dict[str, FieldValue] = {}
    assert compute_section(fields) == BoardSection.STILL_COOKING


def test_compute_section_draft_pr() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: _make_pr(is_draft=True)}
    assert compute_section(fields) == BoardSection.STILL_COOKING


def test_compute_section_merged_pr() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: _make_pr(state=PrState.MERGED)}
    assert compute_section(fields) == BoardSection.PR_MERGED


def test_compute_section_closed_pr() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: _make_pr(state=PrState.CLOSED)}
    assert compute_section(fields) == BoardSection.PR_CLOSED


def test_compute_section_open_pr_no_ci() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: _make_pr()}
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_open_pr_ci_failing() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: CiField(status=CiStatus.FAILING),
    }
    assert compute_section(fields) == BoardSection.PRS_FAILED


def test_compute_section_open_pr_ci_passing() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: CiField(status=CiStatus.PASSING),
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_open_pr_ci_pending() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: CiField(status=CiStatus.PENDING),
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_open_pr_ci_unknown() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: CiField(status=CiStatus.UNKNOWN),
    }
    assert compute_section(fields) == BoardSection.PR_BEING_REVIEWED


def test_compute_section_wrong_muted_type() -> None:
    fields: dict[str, FieldValue] = {FIELD_MUTED: StringField(value="yes")}
    with pytest.raises(KanpanFieldTypeError, match="Expected BoolField"):
        compute_section(fields)


def test_compute_section_wrong_pr_type() -> None:
    fields: dict[str, FieldValue] = {FIELD_PR: StringField(value="oops")}
    with pytest.raises(KanpanFieldTypeError, match="Expected PrField"):
        compute_section(fields)


def test_compute_section_wrong_ci_type() -> None:
    fields: dict[str, FieldValue] = {
        FIELD_PR: _make_pr(),
        FIELD_CI: StringField(value="oops"),
    }
    with pytest.raises(KanpanFieldTypeError, match="Expected CiField"):
        compute_section(fields)


# === _merge_cached_fields ===


def test_merge_cached_fields_empty() -> None:
    assert _merge_cached_fields({}) == {}


def test_merge_cached_fields_single_source() -> None:
    cached: dict[str, dict[AgentName, dict[str, FieldValue]]] = {
        "github": {
            AgentName("a1"): {"pr": _make_pr()},
        },
    }
    merged = _merge_cached_fields(cached)
    assert AgentName("a1") in merged
    assert "pr" in merged[AgentName("a1")]


def test_merge_cached_fields_multiple_sources() -> None:
    pr = _make_pr()
    ci = CiField(status=CiStatus.PASSING)
    cached: dict[str, dict[AgentName, dict[str, FieldValue]]] = {
        "github": {AgentName("a1"): {"pr": pr}},
        "git_info": {AgentName("a1"): {"ci": ci}},
    }
    merged = _merge_cached_fields(cached)
    assert "pr" in merged[AgentName("a1")]
    assert "ci" in merged[AgentName("a1")]
