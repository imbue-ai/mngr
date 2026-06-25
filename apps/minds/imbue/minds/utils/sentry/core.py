import os
from pathlib import Path

from sentry_sdk.integrations.flask import FlaskIntegration

from imbue.imbue_common.sentry.core import MAX_SENTRY_LIST_SIZE
from imbue.imbue_common.sentry.core import flush_sentry_on_shutdown as flush_sentry_on_shutdown
from imbue.imbue_common.sentry.core import setup_sentry as _setup_sentry
from imbue.imbue_common.sentry.data_types import LogAttachmentGroup
from imbue.imbue_common.sentry.data_types import SentryDeployEnvironment
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_minds_root_name_set_to_active_env
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.build_info import resolve_git_sha
from imbue.minds.build_info import resolve_release_id
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR
from imbue.mngr_latchkey.sentry import MNGR_LATCHKEY_SENTRY_S3_UPLOADS_ENV_VAR

# The ``service`` tag / ``server_name`` distinguishing minds-backend events from
# the other Imbue Python processes that report to the same Sentry projects (e.g.
# ``mngr latchkey forward``).
_MINDS_SENTRY_SERVICE_NAME = "minds-backend"

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


# Sentry (backend *and* frontend) is off by default and only turned on when this
# env var is truthy. The Electron launcher / operator opts in explicitly; until
# then nothing is sent to Sentry from either the Python backend or the web UI.
MINDS_SENTRY_ENABLED_ENV_VAR = "MINDS_SENTRY_ENABLED"
# S3 attachment uploads are additionally opt-in (default off, even in
# production/staging) since they can carry potentially-sensitive data.
MINDS_SENTRY_S3_UPLOADS_ENV_VAR = "MINDS_SENTRY_S3_UPLOADS"
_SENTRY_ENABLED_TRUTHY_VALUES = ("1", "true", "yes")


def _is_env_var_truthy(env_var_name: str) -> bool:
    return os.environ.get(env_var_name, "").strip().lower() in _SENTRY_ENABLED_TRUTHY_VALUES


def is_sentry_enabled() -> bool:
    """Whether error reporting is opted in via ``MINDS_SENTRY_ENABLED``.

    Shared by the Python backend (``minds run``) and the web-UI frontend config
    so both honor the same single opt-in switch.
    """
    return _is_env_var_truthy(MINDS_SENTRY_ENABLED_ENV_VAR)


def is_sentry_s3_upload_enabled() -> bool:
    """Whether Sentry S3 attachment uploads are opted in via ``MINDS_SENTRY_S3_UPLOADS``."""
    return _is_env_var_truthy(MINDS_SENTRY_S3_UPLOADS_ENV_VAR)


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


def resolve_latchkey_forward_sentry_env() -> dict[str, str]:
    """Env vars to publish into the detached ``mngr latchkey forward`` supervisor.

    The daemon inherits minds' Sentry opt-in + S3-upload opt-in + environment, plus
    minds' release id / git sha (which it requires to be supplied via env, having no
    fallback of its own), while reading only its own ``MNGR_LATCHKEY_SENTRY_*`` vars.
    """
    return {
        MNGR_LATCHKEY_SENTRY_ENABLED_ENV_VAR: "1" if is_sentry_enabled() else "0",
        MNGR_LATCHKEY_SENTRY_S3_UPLOADS_ENV_VAR: "1" if is_sentry_s3_upload_enabled() else "0",
        MNGR_LATCHKEY_SENTRY_ENVIRONMENT_ENV_VAR: resolve_sentry_environment().value,
        MNGR_LATCHKEY_SENTRY_RELEASE_ENV_VAR: resolve_release_id(),
        MNGR_LATCHKEY_SENTRY_GIT_SHA_ENV_VAR: resolve_git_sha(),
    }


def setup_sentry(
    environment: SentryDeployEnvironment,
    release_id: str,
    git_commit_sha: str,
    log_folder: Path,
    is_s3_upload_enabled: bool = False,
) -> None:
    """Set up Sentry for the minds backend process (Flask integration + flat-log layout)."""
    _setup_sentry(
        environment=environment,
        release_id=release_id,
        git_commit_sha=git_commit_sha,
        log_folder=log_folder,
        service_name=_MINDS_SENTRY_SERVICE_NAME,
        log_attachment_groups=_MINDS_LOG_ATTACHMENT_GROUPS,
        integrations=[FlaskIntegration()],
        is_s3_upload_enabled=is_s3_upload_enabled,
    )
