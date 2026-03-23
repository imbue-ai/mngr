from __future__ import annotations

from typing import Self

from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mng_llm.data_types import LlmSettings
from imbue.mng_mind.data_types import WatcherSettings


class VendorRepoConfig(FrozenModel):
    """Configuration for a single repository to vendor as a git subtree.

    Exactly one of ``url`` or ``path`` must be set:

    - ``url``: a remote git URL (https or ssh)
    - ``path``: a local repository path (absolute, or relative to the current
      working directory of the process creating the mind)

    ``ref`` optionally pins a specific commit, branch, or tag.  When omitted
    the current HEAD of the repository is used.
    """

    name: NonEmptyStr = Field(description="Directory name under vendor/ for this repo.")
    url: str | None = Field(
        default=None,
        description="Remote git URL (mutually exclusive with 'path').",
    )
    path: str | None = Field(
        default=None,
        description="Local repository path (mutually exclusive with 'url').",
    )
    ref: str | None = Field(
        default=None,
        description="Git ref (commit hash, branch, tag). Defaults to current HEAD.",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if self.url is None and self.path is None:
            raise ValueError("exactly one of 'url' or 'path' must be set")
        if self.url is not None and self.path is not None:
            raise ValueError("'url' and 'path' are mutually exclusive")
        return self

    @property
    def is_local(self) -> bool:
        """True when the repo source is a local path."""
        return self.path is not None


class ClaudeMindSettings(LlmSettings):
    """Top-level settings loaded from minds.toml.

    Extends LlmSettings (chat, provisioning) with mind-specific sections
    (agent_type, watchers, vendor). All fields have defaults, so an empty or
    missing settings file produces a valid settings object with the standard
    defaults.
    """

    agent_type: str | None = Field(
        default=None,
        description="Agent type for this mind (e.g. 'elena-code', 'claude-mind'). "
        "Read during agent creation to determine the --type passed to mng create. "
        "Falls back to 'claude-mind' when not set.",
    )
    watchers: WatcherSettings = Field(
        default_factory=WatcherSettings,
        description="Watcher settings ([watchers] section).",
    )
    vendor: tuple[VendorRepoConfig, ...] = Field(
        default=(),
        description="Repositories to vendor as git subtrees when the mind is created. "
        "Each entry is a [[vendor]] table in minds.toml.",
    )
