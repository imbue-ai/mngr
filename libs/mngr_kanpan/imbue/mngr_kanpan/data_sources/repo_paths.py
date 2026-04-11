from collections.abc import Sequence

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import FIELD_REPO_PATH
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import RepoPathField
from imbue.mngr_kanpan.fetcher import repo_path_from_labels


class RepoPathsDataSource:
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
    def field_types(self) -> dict[str, type[FieldValue]]:
        return {FIELD_REPO_PATH: RepoPathField}

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
