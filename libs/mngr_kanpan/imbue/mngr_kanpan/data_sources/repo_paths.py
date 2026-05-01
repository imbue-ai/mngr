from collections.abc import Sequence
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic import TypeAdapter

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FIELD_REPO_PATH
from imbue.mngr_kanpan.data_source import FieldValue


class RepoPathField(FieldValue):
    """GitHub repository path (owner/repo) for an agent."""

    kind: Literal["repo_path"] = Field(default="repo_path", description="Discriminator tag")
    path: str = Field(description="GitHub owner/repo path")

    def display(self) -> CellDisplay:
        return CellDisplay(text=self.path)


@pure
def _parse_github_repo_path(remote_url: str) -> str | None:
    """Extract owner/repo from a GitHub remote URL.

    Supports SSH (git@github.com:owner/repo.git) and
    HTTPS (https://github.com/owner/repo.git) formats.
    """
    # SSH format: git@github.com:owner/repo.git
    if remote_url.startswith("git@github.com:"):
        path = remote_url[len("git@github.com:") :]
        if path.endswith(".git"):
            path = path[:-4]
        return path

    # HTTPS format: https://github.com/owner/repo.git
    parsed = urlparse(remote_url)
    if parsed.hostname == "github.com":
        path = parsed.path.lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return path

    return None


@pure
def repo_path_from_labels(labels: dict[str, str]) -> str | None:
    """Extract GitHub 'owner/repo' from a labels dict's 'remote' entry."""
    remote_url = labels.get("remote")
    if remote_url is None:
        return None
    return _parse_github_repo_path(remote_url)


_REPO_PATH_ADAPTER: TypeAdapter[FieldValue] = TypeAdapter(RepoPathField)


class RepoPathsDataSource(FrozenModel):
    """Computes repo_path field from agent remote labels.

    This is infrastructure data for other data sources (e.g. GitHub).
    Not shown as a column by default.
    """

    @property
    def name(self) -> str:
        return "repo_paths"

    @property
    def is_remote(self) -> bool:
        return False

    @property
    def columns(self) -> dict[str, str]:
        return {FIELD_REPO_PATH: "REPO"}

    @property
    def field_types(self) -> dict[str, TypeAdapter[FieldValue]]:
        return {FIELD_REPO_PATH: _REPO_PATH_ADAPTER}

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]:
        fields: dict[AgentName, dict[str, FieldValue]] = {}
        for agent in agents:
            repo_path = repo_path_from_labels(agent.labels)
            if repo_path is not None:
                fields[agent.name] = {FIELD_REPO_PATH: RepoPathField(path=repo_path)}
        return fields, []
