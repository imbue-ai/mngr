from collections.abc import Callable
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from sentry_sdk.integrations.flask import FlaskIntegration

from imbue.imbue_common.sentry.core import MANUALLY_SUBMITTED_TAG as MANUALLY_SUBMITTED_TAG
from imbue.imbue_common.sentry.core import MAX_SENTRY_LIST_SIZE
from imbue.imbue_common.sentry.core import flush_sentry_on_shutdown as flush_sentry_on_shutdown
from imbue.imbue_common.sentry.core import setup_sentry as _setup_sentry
from imbue.imbue_common.sentry.core import submit_manual_bug_report as submit_manual_bug_report
from imbue.imbue_common.sentry.data_types import LogAttachmentGroup
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_minds_root_name_set_to_active_env
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.build_info import resolve_git_sha
from imbue.minds.build_info import resolve_release_id
from imbue.mngr_latchkey.sentry import ForwardSentryConsent
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_CONSENT_FILE_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR

# The ``service`` tag / ``server_name`` distinguishing minds-backend events from
# the other Imbue Python processes that report to the same Sentry projects (e.g.
# ``mngr latchkey forward``).
_MINDS_SENTRY_SERVICE_NAME = "minds-backend"

# The three minds *Python-backend* Sentry projects. The ``mngr latchkey forward`` daemon (also a
# Python process) reports to the same projects -- minds passes it the resolved DSN via env var --
# distinguishing its events with a ``service`` tag rather than a separate project. These are
# deliberately *not* the minds *frontend* (JavaScript) DSNs, which live in
# :mod:`imbue.minds.utils.sentry.frontend`: a Sentry project is tied to one platform.
SENTRY_DSN_PRODUCTION = (
    "https://d8658891db0c1246864df82eefd74b6d@o4504335315501056.ingest.us.sentry.io/4511609235636224"
)
SENTRY_DSN_STAGING = "https://221f676a7e3c99733e85dc5c8dd6d6e2@o4504335315501056.ingest.us.sentry.io/4511609241862145"
SENTRY_DSN_DEV = "https://0a66e5894c00f701e3c1b7c2daae4650@o4504335315501056.ingest.us.sentry.io/4511609244811264"

# The S3 buckets minds uploads log/traceback attachments to. ``development`` has no bucket.
PRODUCTION_UPLOADS_BUCKET = "traceback-uploads-production"
STAGING_UPLOADS_BUCKET = "traceback-uploads-staging"


class SentryDeployEnvironment(StrEnum):
    """Which minds Python Sentry project (and S3 bucket) a process reports to.

    ``production`` and ``staging`` each report to their own Sentry DSN and S3 bucket;
    ``development`` reports to the shared dev Sentry project and uploads nothing to S3.
    The values are the lowercase Sentry environment names, so this is intentionally a
    plain ``StrEnum`` (not ``UpperCaseStrEnum``).
    """

    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"


_SENTRY_DSN_BY_ENVIRONMENT: Mapping[SentryDeployEnvironment, str] = {
    SentryDeployEnvironment.PRODUCTION: SENTRY_DSN_PRODUCTION,
    SentryDeployEnvironment.STAGING: SENTRY_DSN_STAGING,
    SentryDeployEnvironment.DEVELOPMENT: SENTRY_DSN_DEV,
}

_S3_ATTACHMENT_BUCKET_BY_ENVIRONMENT: Mapping[SentryDeployEnvironment, str | None] = {
    SentryDeployEnvironment.PRODUCTION: PRODUCTION_UPLOADS_BUCKET,
    SentryDeployEnvironment.STAGING: STAGING_UPLOADS_BUCKET,
    SentryDeployEnvironment.DEVELOPMENT: None,
}


# Minds writes all of its logs flat into a single logs directory (``~/.minds/logs``):
#   * ``minds-events.jsonl``       -- the live Python backend log (the loguru JSONL sink)
#   * ``minds-events.jsonl.<ts>``  -- rotated Python backend logs (timestamp-suffixed by make_jsonl_file_sink)
#   * ``minds.log``                -- the Electron main-process log
# None of these are gzip-compressed on disk, so every file is compressed on upload.
_MINDS_LOG_ATTACHMENT_GROUPS = (
    # The live Python backend log (mutable -- re-upload on every report).
    LogAttachmentGroup(
        group_name="live_logs",
        glob="*.jsonl",
        max_file_count=MAX_SENTRY_LIST_SIZE,
        is_compressed=True,
        is_immutable=False,
    ),
    # Rotated Python backend logs (immutable -- upload once and reuse the cached key).
    LogAttachmentGroup(
        group_name="rotated_logs",
        glob="*.jsonl.*",
        max_file_count=1,
        is_compressed=True,
        is_immutable=True,
    ),
    # The Electron main-process log.
    LogAttachmentGroup(
        group_name="electron_logs",
        glob="*.log",
        max_file_count=MAX_SENTRY_LIST_SIZE,
        is_compressed=True,
        is_immutable=False,
    ),
)


def sentry_deploy_environment_from_minds_env_name(env_name: str | None) -> SentryDeployEnvironment:
    """Map an activated minds env name to its Sentry environment.

    Only the exact names ``production`` and ``staging`` get their own targets;
    everything else (``dev-*``, ``ci-*``, or ``None`` when no env is activated)
    falls back to ``DEVELOPMENT``.
    """
    if env_name == SentryDeployEnvironment.PRODUCTION.value:
        return SentryDeployEnvironment.PRODUCTION
    if env_name == SentryDeployEnvironment.STAGING.value:
        return SentryDeployEnvironment.STAGING
    return SentryDeployEnvironment.DEVELOPMENT


def resolve_sentry_environment() -> SentryDeployEnvironment:
    """Select the Sentry environment from the activated minds env in the process env.

    ``production``/``staging`` map to their own targets; everything else (dev-*,
    ci-*, or no activated env) falls back to ``development``. Shared by the
    backend and the frontend so both report under the same environment.
    """
    activated_env_name = (
        env_name_from_root_name(resolve_minds_root_name()) if is_minds_root_name_set_to_active_env() else None
    )
    return sentry_deploy_environment_from_minds_env_name(activated_env_name)


def _s3_attachment_bucket_for_environment(environment: SentryDeployEnvironment) -> str | None:
    return _S3_ATTACHMENT_BUCKET_BY_ENVIRONMENT[environment]


def latchkey_forward_sentry_consent_path(data_dir: Path) -> Path:
    """Path of the JSON consent file minds maintains for the detached ``mngr latchkey forward`` daemon.

    The daemon reads this file live (per event) to gate what it sends, so minds rewrites it whenever
    the user changes their error-reporting consent -- letting a grant/revoke reach the running daemon
    without respawning it.
    """
    return data_dir / "latchkey_forward_sentry_consent.json"


def write_latchkey_forward_sentry_consent(
    consent_file_path: Path,
    is_error_reporting_enabled: bool,
    is_log_inclusion_enabled: bool,
) -> None:
    """Atomically write the daemon's live consent file from minds' current consent settings.

    Called at startup and on every consent change so the detached daemon's live gates reflect the
    user's ``report_unexpected_errors`` / ``include_error_logs`` choices promptly.
    """
    consent = ForwardSentryConsent(
        report_unexpected_errors=is_error_reporting_enabled,
        include_error_logs=is_log_inclusion_enabled,
    )
    consent_file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = consent_file_path.with_suffix(".json.tmp")
    tmp_path.write_text(consent.model_dump_json())
    tmp_path.rename(consent_file_path)


def resolve_latchkey_forward_sentry_env(consent_file_path: Path) -> dict[str, str]:
    """Env vars to publish into the detached ``mngr latchkey forward`` supervisor.

    The daemon receives concrete Sentry *infrastructure* config (the DSN, environment name, and S3
    bucket) that minds resolves from its own (minds-owned) environment model, plus the path of the
    live consent file. The daemon needs no knowledge of minds' Sentry projects/environments -- it just
    reads strings from its ``MNGR_LATCHKEY_SENTRY_*`` vars. The infrastructure is a snapshot taken when
    the supervisor is (re)spawned (it rarely changes); the user-toggleable consent is *not* snapshotted
    here -- it lives in the consent file, which minds rewrites on every change so a grant/revoke reaches
    the running daemon live.
    """
    environment = resolve_sentry_environment()
    bucket = _s3_attachment_bucket_for_environment(environment)
    return {
        MNGR_LATCHKEY_SENTRY_DSN_ENV_VAR: _SENTRY_DSN_BY_ENVIRONMENT[environment],
        MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR: environment.value,
        MNGR_LATCHKEY_SENTRY_S3_BUCKET_ENV_VAR: bucket or "",
        MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR: resolve_release_id(),
        MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR: resolve_git_sha(),
        MNGR_LATCHKEY_SENTRY_CONSENT_FILE_ENV_VAR: str(consent_file_path),
    }


def setup_sentry(
    environment: SentryDeployEnvironment,
    release_id: str,
    git_commit_sha: str,
    log_folder: Path,
    is_error_reporting_enabled: Callable[[], bool],
    is_log_inclusion_enabled: Callable[[], bool],
) -> None:
    """Set up Sentry for the minds backend process (Flask integration + flat-log layout)."""
    _setup_sentry(
        dsn=_SENTRY_DSN_BY_ENVIRONMENT[environment],
        environment_name=environment.value,
        release_id=release_id,
        git_commit_sha=git_commit_sha,
        log_folder=log_folder,
        service_name=_MINDS_SENTRY_SERVICE_NAME,
        log_attachment_groups=_MINDS_LOG_ATTACHMENT_GROUPS,
        integrations=[FlaskIntegration()],
        is_error_reporting_enabled=is_error_reporting_enabled,
        is_log_inclusion_enabled=is_log_inclusion_enabled,
        s3_attachment_bucket=_s3_attachment_bucket_for_environment(environment),
    )
