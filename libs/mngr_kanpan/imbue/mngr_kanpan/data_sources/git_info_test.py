from imbue.mngr_kanpan.data_source import CommitsAheadField
from imbue.mngr_kanpan.data_source import FIELD_COMMITS_AHEAD
from imbue.mngr_kanpan.data_sources.git_info import GitInfoDataSource


def test_git_info_data_source_name() -> None:
    ds = GitInfoDataSource()
    assert ds.name == "git_info"


def test_git_info_columns() -> None:
    ds = GitInfoDataSource()
    assert ds.columns == {FIELD_COMMITS_AHEAD: "GIT"}


def test_git_info_field_types() -> None:
    ds = GitInfoDataSource()
    assert ds.field_types == {FIELD_COMMITS_AHEAD: CommitsAheadField}
