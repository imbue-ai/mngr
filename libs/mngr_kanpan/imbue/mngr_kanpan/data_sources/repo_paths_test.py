from types import SimpleNamespace

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_kanpan.data_source import FIELD_REPO_PATH
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathField
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathsDataSource
from imbue.mngr_kanpan.testing import make_agent_details

_EMPTY_CTX: MngrContext = SimpleNamespace()  # ty: ignore[invalid-assignment]


def test_repo_paths_compute_with_remote_label() -> None:
    ds = RepoPathsDataSource()
    agent = make_agent_details(
        name="agent-1",
        labels={"remote": "git@github.com:org/repo.git"},
    )
    fields, errors = ds.compute(
        agents=(agent,),
        cached_fields={},
        mngr_ctx=_EMPTY_CTX,
    )
    assert len(errors) == 0
    assert agent.name in fields
    repo_field = fields[agent.name][FIELD_REPO_PATH]
    assert isinstance(repo_field, RepoPathField)
    assert repo_field.path == "org/repo"


def test_repo_paths_compute_without_remote_label() -> None:
    ds = RepoPathsDataSource()
    agent = make_agent_details(name="agent-1", labels={})
    fields, errors = ds.compute(
        agents=(agent,),
        cached_fields={},
        mngr_ctx=_EMPTY_CTX,
    )
    assert len(errors) == 0
    assert agent.name not in fields
