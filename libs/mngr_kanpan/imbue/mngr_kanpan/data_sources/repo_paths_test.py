from imbue.mngr_kanpan.data_source import FIELD_REPO_PATH
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathField
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathsDataSource
from imbue.mngr_kanpan.data_sources.repo_paths import _parse_github_repo_path
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_mngr_ctx

# === _parse_github_repo_path ===


def test_parse_ssh_strips_git_suffix() -> None:
    """SSH form with a .git suffix returns owner/repo with .git stripped."""
    assert _parse_github_repo_path("git@github.com:org/repo.git") == "org/repo"


def test_parse_ssh_without_git_suffix() -> None:
    """SSH form without a .git suffix returns owner/repo unchanged."""
    assert _parse_github_repo_path("git@github.com:org/repo") == "org/repo"


def test_parse_https_strips_git_suffix() -> None:
    """HTTPS form with a .git suffix is parsed via urlparse and .git stripped."""
    assert _parse_github_repo_path("https://github.com/org/repo.git") == "org/repo"


def test_parse_https_without_git_suffix() -> None:
    """HTTPS form without a .git suffix returns owner/repo unchanged."""
    assert _parse_github_repo_path("https://github.com/org/repo") == "org/repo"


def test_parse_non_github_host_returns_none() -> None:
    """A non-github.com host falls through to the `return None` branch."""
    assert _parse_github_repo_path("https://gitlab.com/org/repo.git") is None


# === compute ===


def test_repo_paths_compute_with_remote_label() -> None:
    ds = RepoPathsDataSource()
    agent = make_agent_details(
        name="agent-1",
        labels={"remote": "git@github.com:org/repo.git"},
    )
    fields, errors = ds.compute(
        agents=(agent,),
        cached_fields={},
        mngr_ctx=make_mngr_ctx(),
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
        mngr_ctx=make_mngr_ctx(),
    )
    assert len(errors) == 0
    assert agent.name not in fields
