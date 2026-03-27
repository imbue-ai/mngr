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


def push_files(
    agent: AgentInterface,
    host: OnlineHostInterface,
    source: Path,
    destination_path: Path | None,
    is_dry_run: bool,
    is_delete: bool,
    uncommitted_changes: UncommittedChangesMode,
    cg: ConcurrencyGroup,
) -> SyncFilesResult:
    """Push files from a local directory to an agent's work directory using rsync."""
    return sync_files(
        agent=agent,
        host=host,
        mode=SyncMode.PUSH,
        local_path=source,
        remote_path=destination_path,
        is_dry_run=is_dry_run,
        is_delete=is_delete,
        uncommitted_changes=uncommitted_changes,
        cg=cg,
    )


def push_git(
    agent: AgentInterface,
    host: OnlineHostInterface,
    source: Path,
    source_branch: str | None,
    target_branch: str | None,
    is_dry_run: bool,
    uncommitted_changes: UncommittedChangesMode,
    is_mirror: bool,
    cg: ConcurrencyGroup,
) -> SyncGitResult:
    """Push git commits from a local repository to an agent's repository."""
    return sync_git(
        agent=agent,
        host=host,
        mode=SyncMode.PUSH,
        local_path=source,
        source_branch=source_branch,
        target_branch=target_branch,
        is_dry_run=is_dry_run,
        uncommitted_changes=uncommitted_changes,
        is_mirror=is_mirror,
        cg=cg,
    )
