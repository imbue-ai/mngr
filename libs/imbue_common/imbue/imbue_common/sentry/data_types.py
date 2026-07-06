from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class LogAttachmentGroup(FrozenModel):
    """Describes one group of on-disk log files to attach to an error report.

    Each calling process supplies its own set of groups describing its log
    layout (e.g. minds' flat ``~/.minds/logs`` directory, or the
    ``mngr latchkey forward`` plugin data dir). The Sentry error pipeline globs
    ``glob`` under the process's log folder, keeps the ``max_file_count`` newest
    matches, optionally gzip-compresses them, and uploads them under
    ``group_name`` in the event's ``extra``.
    """

    group_name: str = Field(
        description="Logical name the uploaded files are grouped under in the event extra (e.g. ``live_logs``)."
    )
    glob: str = Field(description="Glob (relative to the process's log folder) selecting the files in this group.")
    max_file_count: int = Field(description="Keep at most this many newest matching files per error report.")
    is_compressed: bool = Field(description="Whether to gzip-compress each file before uploading it to S3.")
    is_immutable: bool = Field(
        description=(
            "Whether the matched files never change once written (e.g. rotated logs). Immutable files are "
            "uploaded once and the S3 key is cached and reused on later reports; mutable files (e.g. the live "
            "log) are re-uploaded every report."
        )
    )
