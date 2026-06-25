from collections.abc import Mapping
from enum import StrEnum

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

# The three shared Imbue *Python-backend* Sentry projects. These were originally
# created for the minds Python backend; the ``mngr latchkey forward`` daemon (also
# a Python process) reports to the same projects, distinguishing its events with a
# ``service`` tag and its own ``server_name`` rather than a separate project.
#
# These are deliberately *not* the minds *frontend* (JavaScript) DSNs, which live
# in ``imbue.minds.utils.sentry.frontend``: a single Sentry project is tied to one
# platform (issue grouping, source-map handling, release-health UI are all
# platform-specific), so the Python and JavaScript SDKs stay on separate projects.
SENTRY_DSN_PRODUCTION = (
    "https://d8658891db0c1246864df82eefd74b6d@o4504335315501056.ingest.us.sentry.io/4511609235636224"
)
SENTRY_DSN_STAGING = "https://221f676a7e3c99733e85dc5c8dd6d6e2@o4504335315501056.ingest.us.sentry.io/4511609241862145"
SENTRY_DSN_DEV = "https://0a66e5894c00f701e3c1b7c2daae4650@o4504335315501056.ingest.us.sentry.io/4511609244811264"


class SentryDeployEnvironment(StrEnum):
    """Which shared Imbue Python Sentry project (and S3 bucket) a process reports to.

    ``production`` and ``staging`` each report to their own Sentry DSN and S3
    bucket; ``development`` reports to the shared dev Sentry project and uploads
    nothing to S3. The values are the lowercase Sentry environment names, so this
    is intentionally a plain ``StrEnum`` (not ``UpperCaseStrEnum``).
    """

    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"


# The DSN each environment reports to. Shared by every Python process (minds
# backend and ``mngr latchkey forward``) that calls ``setup_sentry``.
SENTRY_DSN_BY_ENVIRONMENT: Mapping[SentryDeployEnvironment, str] = {
    SentryDeployEnvironment.PRODUCTION: SENTRY_DSN_PRODUCTION,
    SentryDeployEnvironment.STAGING: SENTRY_DSN_STAGING,
    SentryDeployEnvironment.DEVELOPMENT: SENTRY_DSN_DEV,
}


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
