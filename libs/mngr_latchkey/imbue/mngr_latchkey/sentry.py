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
#
# These describe the (mostly static) *infrastructure*: which project to report to and how
# the build is tagged. They are snapshotted into the daemon's environment when it is
# spawned. The *consent* settings -- whether to actually report, and whether to attach
# logs -- live instead in the consent file at ``MNGR_LATCHKEY_SENTRY_CONSENT_FILE`` so the
# embedder can toggle them on a running daemon (the gates read the file live, per event).
MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR = "MNGR_LATCHKEY_SENTRY_DSN"
MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR = "MNGR_LATCHKEY_SENTRY_ENVIRONMENT"
# The S3 bucket to upload log/traceback attachments to. Empty / unset means there is no bucket
# (e.g. a development environment), so nothing is ever uploaded regardless of consent.
MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR = "MNGR_LATCHKEY_SENTRY_S3_BUCKET"
MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR = "MNGR_LATCHKEY_SENTRY_RELEASE"
MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR = "MNGR_LATCHKEY_SENTRY_GIT_SHA"
# Path to the JSON consent file the embedder writes (and rewrites whenever the user toggles
# consent). The daemon reads it live on every event, so a grant/revoke takes effect without
# respawning the daemon. Absent/unreadable file -> reporting and log inclusion both off.
MNGR_LATCHKEY_SENTRY_CONSENT_FILE_ENV_VAR = "MNGR_LATCHKEY_SENTRY_CONSENT_FILE"

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


class ForwardSentryConsent(FrozenModel):
    """The user-consent toggles that live-gate the daemon's Sentry reporting.

    Mirrors the minds backend's ``report_unexpected_errors`` / ``include_error_logs`` user settings.
    The embedder writes this as JSON to the consent file; the daemon reads it live on every event.
    Shared as the serialization contract between the embedder (writer) and the daemon (reader).
    """

    report_unexpected_errors: bool = Field(description="Whether automatic error reports may be sent.")
    include_error_logs: bool = Field(description="Whether log/traceback attachments may be uploaded.")


_NO_CONSENT = ForwardSentryConsent(report_unexpected_errors=False, include_error_logs=False)


def read_forward_sentry_consent(consent_file_path: Path | None) -> ForwardSentryConsent:
    """Read the live consent toggles, defaulting to all-off when absent/unreadable/malformed.

    Robust by design: this runs inside Sentry's before_send / attachment hooks (once per event), so
    it must never raise -- any failure is treated as "no consent" so the daemon errs toward not
    reporting.
    """
    if consent_file_path is None:
        return _NO_CONSENT
    try:
        raw = consent_file_path.read_text()
    except OSError:
        return _NO_CONSENT
    try:
        return ForwardSentryConsent.model_validate_json(raw)
    except ValueError:
        return _NO_CONSENT


class ForwardSentryConfig(FrozenModel):
    """Resolved (mostly static) Sentry infrastructure config for the ``mngr latchkey forward`` daemon.

    The live, user-toggleable consent is read separately from :attr:`consent_file_path`.
    """

    dsn: str = Field(description="The Sentry DSN to report to (resolved and supplied by the embedder).")
    environment_name: str = Field(description="The Sentry environment label (e.g. ``production``/``staging``).")
    release_id: str = Field(description="Release version the running code was cut from (inherited from the embedder).")
    git_commit_sha: str = Field(description="Git SHA the running code was cut from (inherited from the embedder).")
    s3_attachment_bucket: str | None = Field(
        description="S3 bucket for log/traceback attachments, or ``None`` when the environment has no bucket."
    )
    consent_file_path: Path | None = Field(
        description="Path to the JSON consent file the daemon reads live, or ``None`` if the embedder published none."
    )


def resolve_forward_sentry_config() -> ForwardSentryConfig | None:
    """Resolve the daemon's Sentry infrastructure config from its ``MNGR_LATCHKEY_SENTRY_*`` env vars.

    Returns ``None`` (and logs why) when the daemon is not configured for Sentry -- i.e. when run
    standalone rather than spawned by an embedder, so the required env vars are absent. Unlike minds,
    the daemon has no fallback for the DSN / environment / release id / git sha: they are required to
    be supplied via env vars, so a partial/misconfigured environment disables reporting rather than
    inventing placeholder values. The S3 bucket and consent file are optional (an empty bucket means
    no uploads; an absent consent file means no reporting until the embedder writes one).
    """
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
        logger.info(
            "Sentry is not configured for mngr latchkey forward (missing {}); skipping Sentry setup.",
            ", ".join(missing_env_var_names),
        )
        return None

    bucket = os.environ.get(MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR, "").strip()
    consent_file = os.environ.get(MNGR_LATCHKEY_SENTRY_CONSENT_FILE_ENV_VAR, "").strip()
    return ForwardSentryConfig(
        dsn=dsn,
        environment_name=environment_name,
        release_id=release_id,
        git_commit_sha=git_commit_sha,
        s3_attachment_bucket=bucket or None,
        consent_file_path=Path(consent_file) if consent_file else None,
    )


def setup_forward_sentry(log_folder: Path) -> None:
    """Initialize Sentry for the ``mngr latchkey forward`` daemon when configured via env vars.

    No-op (beyond logging) when the daemon is not configured for Sentry (e.g. run standalone). Must be
    called *after* the command's loguru sinks are set up so Sentry layers on top.

    Sentry always initializes when configured; what it actually *sends* is gated live by the consent
    file the embedder maintains, exactly mirroring how the minds backend gates its own Sentry on live
    user settings. So a grant/revoke by the user reaches this detached daemon on the next event,
    without respawning it. With no consent file (or an unreadable one), both gates default to off.
    """
    config = resolve_forward_sentry_config()
    if config is None:
        return
    consent_file_path = config.consent_file_path
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
        is_error_reporting_enabled=lambda: read_forward_sentry_consent(consent_file_path).report_unexpected_errors,
        is_log_inclusion_enabled=lambda: read_forward_sentry_consent(consent_file_path).include_error_logs,
        s3_attachment_bucket=config.s3_attachment_bucket,
    )
