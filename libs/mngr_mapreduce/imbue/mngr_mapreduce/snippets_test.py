"""Unit tests for framework prompt snippets."""

from imbue.mngr_mapreduce.snippets import ARCHIVE_FILENAME
from imbue.mngr_mapreduce.snippets import ARCHIVE_SUBDIR
from imbue.mngr_mapreduce.snippets import ARCHIVE_SUBPATH
from imbue.mngr_mapreduce.snippets import atomic_write_snippet
from imbue.mngr_mapreduce.snippets import publish_outputs_snippet


def test_archive_subpath_composition() -> None:
    assert ARCHIVE_SUBPATH == f"{ARCHIVE_SUBDIR}/{ARCHIVE_FILENAME}"


def test_publish_outputs_snippet_contains_archive_path() -> None:
    snippet = publish_outputs_snippet()
    assert ARCHIVE_SUBDIR in snippet
    assert ARCHIVE_FILENAME in snippet
    # The .tmp + mv idiom is what makes the upload safe for concurrent reads.
    assert ".tmp" in snippet
    assert "mv " in snippet
    # The branch.bundle inclusion only happens when there are commits past the base.
    assert "branch.bundle" in snippet
    assert "MNGR_GIT_BASE_BRANCH" in snippet


def test_atomic_write_snippet_default_var() -> None:
    snippet = atomic_write_snippet(".test_output/foo.json")
    assert "OUTCOME_JSON" in snippet
    assert ".test_output/foo.json.draft" in snippet
    assert ".test_output/foo.json" in snippet


def test_atomic_write_snippet_custom_var() -> None:
    snippet = atomic_write_snippet("path.json", content_var="MY_VAR")
    assert "MY_VAR" in snippet
    assert "OUTCOME_JSON" not in snippet
