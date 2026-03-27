from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.api.sync import SyncFilesResult
from imbue.mng.api.sync import SyncGitResult
from imbue.mng.api.sync import sync_files
from imbue.mng.api.sync import sync_git
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import SyncMode
from imbue.mng.primitives import UncommittedChangesMode


def pull_files(
    agent: AgentInterface,
    host: OnlineHostInterface,
    destination: Path,
    source_path: Path | None,
    is_dry_run: bool,
    is_delete: bool,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> SyncFilesResult:
    """Pull files from an agent's work directory to a local directory using rsync."""
    return sync_files(
        agent=agent,
        host=host,
        mode=SyncMode.PULL,
        local_path=destination,
        remote_path=source_path,
        is_dry_run=is_dry_run,
        is_delete=is_delete,
        uncommitted_changes=uncommitted_changes,
        cg=cg,
    )


def pull_git(
    agent: AgentInterface,
    host: OnlineHostInterface,
    destination: Path,
    source_branch: str | None,
    target_branch: str | None,
    is_dry_run: bool,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> SyncGitResult:
    """Pull git commits from an agent's repository by merging branches."""
    return sync_git(
        agent=agent,
        host=host,
        mode=SyncMode.PULL,
        local_path=destination,
        source_branch=source_branch,
        target_branch=target_branch,
        is_dry_run=is_dry_run,
        uncommitted_changes=uncommitted_changes,
        is_mirror=False,
        cg=cg,
    )
