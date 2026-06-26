import os
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.sentry.core import MAX_SENTRY_LIST_SIZE
from imbue.imbue_common.sentry.core import setup_sentry
from imbue.imbue_common.sentry.data_types import LogAttachmentGroup

# The ``service`` tag / ``server_name`` distinguishing ``mngr latchkey forward``
# events from the other Imbue Python processes (e.g. the minds backend) that
# report to the same Sentry projects.
_FORWARD_SENTRY_SERVICE_NAME = "mngr-latchkey-forward"

# Env vars the ``mngr latchkey forward`` daemon reads to configure Sentry. They are
# deliberately namespaced ``MNGR_LATCHKEY_*`` (not ``LATCHKEY_*``) so they are never
# confused with the upstream core ``latchkey`` project's own configuration. The daemon
# does not know about any particular Sentry project or environment: it receives concrete
# config (the DSN, the environment label, the S3 bucket) as strings, which the embedder
# (the minds desktop client) resolves from its own settings and publishes here.
MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR = "MNGR_LATCHKEY_SENTRY_ENABLED"
MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR = "MNGR_LATCHKEY_SENTRY_DSN"
MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR = "MNGR_LATCHKEY_SENTRY_ENVIRONMENT"
# The S3 bucket to upload log/traceback attachments to. Empty / unset means upload nothing.
MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR = "MNGR_LATCHKEY_SENTRY_S3_BUCKET"
MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR = "MNGR_LATCHKEY_SENTRY_RELEASE"
MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR = "MNGR_LATCHKEY_SENTRY_GIT_SHA"

_SENTRY_ENABLED_TRUTHY_VALUES = ("1", "true", "yes")

# The ``mngr latchkey forward`` process writes its structured loguru log to
# ``events.jsonl`` (rotated to ``events.jsonl.<ts>``) and its raw stdout/stderr
# capture to ``latchkey_forward.log``, all flat in the plugin data dir. None are
# gzip-compressed on disk, so every file is compressed on upload.
_FORWARD_LOG_ATTACHMENT_GROUPS = (
    # The live structured log (mutable -- re-upload on every report).
    LogAttachmentGroup(
        group_name="live_logs",
        glob="*.jsonl",
        max_file_count=MAX_SENTRY_LIST_SIZE,
        is_compressed=True,
        is_immutable=False,
    ),
    # Rotated structured logs (immutable -- upload once and reuse the cached key).
    LogAttachmentGroup(
        group_name="rotated_logs",
        glob="*.jsonl.*",
        max_file_count=1,
        is_compressed=True,
        is_immutable=True,
    ),
    # The raw stdout/stderr capture log.
    LogAttachmentGroup(
        group_name="raw_logs",
        glob="*.log",
        max_file_count=MAX_SENTRY_LIST_SIZE,
        is_compressed=True,
        is_immutable=False,
    ),
)


def _is_env_var_truthy(env_var_name: str) -> bool:
    return os.environ.get(env_var_name, "").strip().lower() in _SENTRY_ENABLED_TRUTHY_VALUES


class ForwardSentryConfig(FrozenModel):
    """Resolved Sentry configuration for the ``mngr latchkey forward`` daemon."""

    dsn: str = Field(description="The Sentry DSN to report to (resolved and supplied by the embedder).")
    environment_name: str = Field(description="The Sentry environment label (e.g. ``production``/``staging``).")
    release_id: str = Field(description="Release version the running code was cut from (inherited from the embedder).")
    git_commit_sha: str = Field(description="Git SHA the running code was cut from (inherited from the embedder).")
    s3_attachment_bucket: str | None = Field(
        description="S3 bucket for log/traceback attachments, or ``None`` to upload nothing."
    )


def resolve_forward_sentry_config() -> ForwardSentryConfig | None:
    """Resolve the daemon's Sentry config from its ``MNGR_LATCHKEY_SENTRY_*`` env vars.

    Returns ``None`` (and logs why) when reporting is disabled or the required inputs are missing.
    Unlike minds, the daemon has no fallback for the DSN / environment / release id / git sha: they
    are required to be supplied via env vars by the embedder (the minds desktop client), so a
    misconfigured environment disables reporting rather than inventing placeholder values. The S3
    bucket is optional -- an empty value means uploads are off.
    """
    if not _is_env_var_truthy(MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR):
        logger.info(
            "Sentry is disabled for mngr latchkey forward (set {}=1 to enable error reporting).",
            MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR,
        )
        return None

    dsn = os.environ.get(MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR, "").strip()
    environment_name = os.environ.get(MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR, "").strip()
    release_id = os.environ.get(MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR, "").strip()
    git_commit_sha = os.environ.get(MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR, "").strip()

    missing_env_var_names = [
        name
        for name, value in (
            (MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR, dsn),
            (MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR, environment_name),
            (MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR, release_id),
            (MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR, git_commit_sha),
        )
        if not value
    ]
    if missing_env_var_names:
        logger.error(
            "Sentry is enabled for mngr latchkey forward but required env vars are missing ({}); "
            "skipping Sentry setup.",
            ", ".join(missing_env_var_names),
        )
        return None

    bucket = os.environ.get(MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR, "").strip()
    return ForwardSentryConfig(
        dsn=dsn,
        environment_name=environment_name,
        release_id=release_id,
        git_commit_sha=git_commit_sha,
        s3_attachment_bucket=bucket or None,
    )


def setup_forward_sentry(log_folder: Path) -> None:
    """Initialize Sentry for the ``mngr latchkey forward`` daemon if opted in via env vars.

    No-op (beyond logging) when reporting is disabled or misconfigured. Must be
    called *after* the command's loguru sinks are set up so Sentry layers on top.

    Unlike the minds backend -- whose error-reporting / log-inclusion gates are read
    live from a user setting that can be toggled at runtime -- the detached daemon
    has no live setting to consult: its env vars are a snapshot taken when the
    minds desktop client (re)spawned it. So both gates are constant for the
    process: error reporting is on whenever Sentry was set up at all, and log
    inclusion follows the snapshotted bucket (present -> on).
    """
    config = resolve_forward_sentry_config()
    if config is None:
        return
    is_log_inclusion_enabled = config.s3_attachment_bucket is not None
    setup_sentry(
        dsn=config.dsn,
        environment_name=config.environment_name,
        release_id=config.release_id,
        git_commit_sha=config.git_commit_sha,
        log_folder=log_folder,
        service_name=_FORWARD_SENTRY_SERVICE_NAME,
        log_attachment_groups=_FORWARD_LOG_ATTACHMENT_GROUPS,
        # The daemon is not a web app: it needs no Flask (or other) integration
        # beyond Sentry's default integrations.
        integrations=[],
        is_error_reporting_enabled=lambda: True,
        is_log_inclusion_enabled=lambda: is_log_inclusion_enabled,
        s3_attachment_bucket=config.s3_attachment_bucket,
    )
