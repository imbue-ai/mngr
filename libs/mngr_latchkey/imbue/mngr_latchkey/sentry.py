import os
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.sentry.core import MAX_SENTRY_LIST_SIZE
from imbue.imbue_common.sentry.core import setup_sentry
from imbue.imbue_common.sentry.data_types import LogAttachmentGroup
from imbue.imbue_common.sentry.data_types import SentryDeployEnvironment

# The ``service`` tag / ``server_name`` distinguishing ``mngr latchkey forward``
# events from the other Imbue Python processes (e.g. the minds backend) that
# report to the same shared Sentry projects.
_FORWARD_SENTRY_SERVICE_NAME = "mngr-latchkey-forward"

# Env vars the ``mngr latchkey forward`` daemon reads to configure Sentry. They are
# deliberately namespaced ``MNGR_LATCHKEY_*`` (not ``LATCHKEY_*``) so they are never
# confused with the upstream core ``latchkey`` project's own configuration. The
# minds desktop client publishes these into the detached supervisor's environment,
# derived from its own (``MINDS_SENTRY_*``) values, so the daemon "inherits" minds'
# opt-in + environment while reading only its own variables.
MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR = "MNGR_LATCHKEY_SENTRY_ENABLED"
MNGR_LATCHKEY_SENTRY_S3_UPLOADS_ENV_VAR = "MNGR_LATCHKEY_SENTRY_S3_UPLOADS"
MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR = "MNGR_LATCHKEY_SENTRY_ENVIRONMENT"
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

    environment: SentryDeployEnvironment = Field(description="Which shared Imbue Python Sentry project to report to.")
    release_id: str = Field(description="Release version the running code was cut from (inherited from the embedder).")
    git_commit_sha: str = Field(description="Git SHA the running code was cut from (inherited from the embedder).")
    is_s3_upload_enabled: bool = Field(description="Whether to upload log/traceback attachments to S3.")


def resolve_forward_sentry_config() -> ForwardSentryConfig | None:
    """Resolve the daemon's Sentry config from its ``MNGR_LATCHKEY_SENTRY_*`` env vars.

    Returns ``None`` (and logs why) when reporting is disabled or the required
    inputs are missing/invalid. Unlike minds, the daemon has no fallback for the
    release id / git sha: they are required to be supplied via env vars by the
    embedder (the minds desktop client), so a misconfigured environment disables
    reporting rather than inventing placeholder values.
    """
    if not _is_env_var_truthy(MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR):
        logger.info(
            "Sentry is disabled for mngr latchkey forward (set {}=1 to enable error reporting).",
            MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR,
        )
        return None

    environment_value = os.environ.get(MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR, "").strip()
    release_id = os.environ.get(MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR, "").strip()
    git_commit_sha = os.environ.get(MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR, "").strip()

    missing_env_var_names = [
        name
        for name, value in (
            (MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR, environment_value),
            (MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR, release_id),
            (MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR, git_commit_sha),
        )
        if not value
    ]
    if missing_env_var_names:
        logger.warning(
            "Sentry is enabled for mngr latchkey forward but required env vars are missing ({}); "
            "skipping Sentry setup.",
            ", ".join(missing_env_var_names),
        )
        return None

    try:
        environment = SentryDeployEnvironment(environment_value)
    except ValueError:
        logger.warning(
            "Sentry is enabled for mngr latchkey forward but {}={!r} is not a valid environment "
            "(expected one of {}); skipping Sentry setup.",
            MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR,
            environment_value,
            ", ".join(member.value for member in SentryDeployEnvironment),
        )
        return None

    return ForwardSentryConfig(
        environment=environment,
        release_id=release_id,
        git_commit_sha=git_commit_sha,
        is_s3_upload_enabled=_is_env_var_truthy(MNGR_LATCHKEY_SENTRY_S3_UPLOADS_ENV_VAR),
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
    inclusion follows the snapshotted ``MNGR_LATCHKEY_SENTRY_S3_UPLOADS`` value.
    """
    config = resolve_forward_sentry_config()
    if config is None:
        return
    is_s3_upload_enabled = config.is_s3_upload_enabled
    setup_sentry(
        environment=config.environment,
        release_id=config.release_id,
        git_commit_sha=config.git_commit_sha,
        log_folder=log_folder,
        service_name=_FORWARD_SENTRY_SERVICE_NAME,
        log_attachment_groups=_FORWARD_LOG_ATTACHMENT_GROUPS,
        # The daemon is not a web app: it needs no Flask (or other) integration
        # beyond Sentry's default integrations.
        integrations=[],
        is_error_reporting_enabled=lambda: True,
        is_log_inclusion_enabled=lambda: is_s3_upload_enabled,
    )
