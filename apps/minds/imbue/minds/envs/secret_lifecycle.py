"""Timestamped Modal Secret naming + GC for ``minds env deploy``.

Every ``minds env deploy`` mints a :class:`DeployId` (UTC timestamp,
ISO-compact format ``YYYYMMDDTHHMMSSZ``) and pushes its per-env Modal
Secrets under ``<service>-<tier>-<deploy_id>`` names (never overwrites
an existing Secret). The deployed Modal apps read ``MINDS_DEPLOY_ID``
at module load and reference exactly the matching Secret names via
``Secret.from_name(...)``, so ``modal app rollback <app> <prior>``
re-attaches the rolled-back code to the matching prior Secret set.

After a successful deploy, :func:`gc_old_per_tier_secrets` walks
``modal secret list`` and deletes everything older than the most-recent
``keep_last`` per ``<service>-<tier>``. Cleanup failures are logged but
never fail the deploy (the recover-target file has already been
deleted at that point).
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from datetime import timezone
from typing import Final
from typing import Self

from loguru import logger
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.errors import MindError

# ``YYYYMMDDTHHMMSSZ`` -- compact ISO 8601 (RFC 3339 without separators),
# always UTC, lex sort = chron sort. No timezone offset other than Z is
# accepted because the runtime always builds the id via :func:`make_deploy_id`.
# Two regexes: one for validating a standalone id (anchored), one for
# pulling an id off the trailing position in a Secret name (unanchored,
# ``re.search``-friendly with an end-of-string anchor).
_DEPLOY_ID_BODY: Final[str] = r"\d{8}T\d{6}Z"
_DEPLOY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^{_DEPLOY_ID_BODY}$")
_DEPLOY_ID_TRAILING_PATTERN: Final[re.Pattern[str]] = re.compile(rf"{_DEPLOY_ID_BODY}$")
_DEPLOY_ID_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"

# Max number of timestamped Secrets to keep per ``<service>-<tier>`` after a
# successful deploy. Older ones get deleted by :func:`gc_old_per_tier_secrets`.
DEFAULT_KEEP_LAST_SECRETS: Final[int] = 10

# Env var the deployed Modal apps read at module load to identify which
# timestamped Secret bundle to attach. The deploy subprocess threads
# this in via :mod:`per_env_deploy`'s ``modal deploy`` shellout. The
# deployed apps hard-fail (raise :class:`DeployIdMissingError`) when
# this is unset -- there's no fallback to an unsuffixed secret name.
MINDS_DEPLOY_ID_ENV_VAR: Final[str] = "MINDS_DEPLOY_ID"

# Timeout for ``modal secret list --json``. Modal's list endpoint can be
# slow when an env has hundreds of secrets; keep this generous enough to
# absorb that.
_MODAL_SECRET_LIST_TIMEOUT_SECONDS: Final[float] = 60.0


class InvalidDeployIdError(MindError):
    """Raised when a :class:`DeployId` literal does not match the expected pattern."""


class DeployIdMissingError(MindError):
    """Raised when ``MINDS_DEPLOY_ID`` is missing from the deployed Modal app's env.

    Surfaced at module load inside the Modal apps (``apps/modal_litellm/app.py``
    and ``apps/remote_service_connector/.../app.py``) when ``minds env deploy``
    forgot to thread the id through, or when the app was deployed by something
    other than ``minds env deploy`` (which isn't supported under the
    timestamped-secret model).
    """


class DeployId(NonEmptyStr):
    """UTC ISO-compact deploy id, format ``YYYYMMDDTHHMMSSZ``.

    Lex sort == chronological sort. Used as a suffix on per-deploy
    Modal Secret names (``<service>-<tier>-<deploy_id>``) and threaded
    into the deployed Modal app's env so it can attach to the matching
    Secret bundle at module load.
    """

    def __new__(cls, value: str) -> Self:
        stripped = value.strip()
        if not _DEPLOY_ID_PATTERN.fullmatch(stripped):
            raise InvalidDeployIdError(
                f"Invalid deploy id {value!r}: must match {_DEPLOY_ID_PATTERN.pattern!r} "
                "(compact ISO 8601 UTC, e.g. '20260517T143022Z')."
            )
        return super().__new__(cls, stripped)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(pattern=_DEPLOY_ID_PATTERN.pattern),
        )


def make_deploy_id(now: datetime | None = None) -> DeployId:
    """Mint a fresh :class:`DeployId` from the current UTC time (or ``now`` if given).

    Tests pass an explicit ``now`` to pin the id; production code calls
    with no argument.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise InvalidDeployIdError("make_deploy_id requires a tz-aware datetime; got a naive one.")
    return DeployId(now.astimezone(timezone.utc).strftime(_DEPLOY_ID_FORMAT))


def timestamped_secret_name(service: str, tier: str, deploy_id: DeployId) -> str:
    """Return the Modal Secret name for one ``(service, tier, deploy_id)`` triple."""
    return f"{service}-{tier}-{deploy_id}"


def parse_timestamped_secret_name(name: str, *, tier: str) -> tuple[str, DeployId] | None:
    """Parse ``<service>-<tier>-<deploy_id>`` -> ``(service, deploy_id)``.

    Returns ``None`` for names that don't match the shape -- callers
    (notably :func:`gc_old_per_tier_secrets`) use that as the signal to
    skip the entry.
    """
    deploy_id_match = _DEPLOY_ID_TRAILING_PATTERN.search(name)
    if deploy_id_match is None:
        return None
    deploy_id_str = deploy_id_match.group(0)
    suffix = f"-{tier}-{deploy_id_str}"
    if not name.endswith(suffix):
        return None
    service = name[: -len(suffix)]
    if not service:
        return None
    try:
        return service, DeployId(deploy_id_str)
    except InvalidDeployIdError:
        return None


def list_modal_secrets(*, modal_env: str, parent_cg: ConcurrencyGroup) -> tuple[str, ...]:
    """Return every Modal Secret name in ``modal_env`` (via ``modal secret list --json``).

    Surfaces a :class:`ModalDeployError` on non-zero exit so the caller
    knows the cleanup couldn't run (the deploy itself is unaffected --
    cleanup is best-effort).
    """
    command = ["modal", "secret", "list", "--env", modal_env, "--json"]
    cg = parent_cg.make_concurrency_group(name=f"modal-secret-list-{modal_env}")
    with cg:
        result = cg.run_process_to_completion(
            command=command,
            timeout=_MODAL_SECRET_LIST_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=dict(os.environ),
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ModalDeployError(f"`modal secret list --env {modal_env}` failed (exit {result.returncode}): {stderr}")
    try:
        rows = json.loads(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ModalDeployError(f"`modal secret list --json` returned non-JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ModalDeployError(f"`modal secret list --json` returned a non-list payload: {rows!r}")
    names: list[str] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("Name"), str):
            names.append(row["Name"])
        elif isinstance(row, dict) and isinstance(row.get("name"), str):
            names.append(row["name"])
        else:
            # Modal CLI's output shape has shifted across versions; skip
            # rows that don't carry a usable Name / name field instead of
            # raising (we lose visibility into them in the GC, but the
            # operator can still inspect the env via the Modal dashboard).
            continue
    return tuple(names)


def gc_old_per_tier_secrets(
    *,
    modal_env: str,
    tier: str,
    list_modal_secrets_fn,
    delete_modal_secret_fn,
    keep_last: int = DEFAULT_KEEP_LAST_SECRETS,
    parent_cg: ConcurrencyGroup,
) -> None:
    """Delete timestamped Modal Secrets older than the last ``keep_last`` per service.

    Lists every Secret in ``modal_env``, filters to names matching
    ``<service>-<tier>-<YYYYMMDDTHHMMSSZ>``, groups by service, sorts
    each group's deploy ids lexicographically (== chronological), and
    deletes everything older than the most-recent ``keep_last``.
    Secrets whose names don't match the timestamped shape are left
    alone.

    Both callbacks are injected so the same function works against
    fakes in tests + the real Providers bundle at runtime:

    * ``list_modal_secrets_fn(modal_env: str, parent_cg: ConcurrencyGroup) -> tuple[str, ...]``
    * ``delete_modal_secret_fn(secret_name: str, modal_env: str, parent_cg: ConcurrencyGroup) -> None``

    Both signatures match the Providers field signatures so the runtime
    can pass them through directly (no wrapper / nested-function
    indirection).

    Best-effort: a single secret-delete failure is logged but does not
    abort the GC pass -- we still try to delete the remaining old ones.
    """
    if keep_last < 0:
        raise MindError(f"keep_last must be >= 0; got {keep_last!r}.")
    all_names = list_modal_secrets_fn(modal_env, parent_cg)
    by_service: dict[str, list[DeployId]] = defaultdict(list)
    for name in all_names:
        parsed = parse_timestamped_secret_name(name, tier=tier)
        if parsed is None:
            continue
        service, deploy_id = parsed
        by_service[service].append(deploy_id)

    for service, deploy_ids in by_service.items():
        # Sort ascending; the LAST ``keep_last`` are the newest. When
        # ``keep_last == 0`` (the destroy-all-tier-secrets path), every
        # match gets deleted.
        deploy_ids.sort()
        to_delete = deploy_ids if keep_last == 0 else deploy_ids[:-keep_last] if len(deploy_ids) > keep_last else []
        for deploy_id in to_delete:
            secret_name = timestamped_secret_name(service, tier, deploy_id)
            try:
                delete_modal_secret_fn(secret_name, modal_env, parent_cg)
            except ModalDeployError as exc:
                logger.warning(
                    "GC: failed to delete old Modal Secret {!r} in env {!r}: {}", secret_name, modal_env, exc
                )
