"""Finding smoke accounts that earlier runs left behind.

A run whose teardown never fired (a cancelled job, a crashed runner) leaves its
throwaway account on the tier. Every smoke address carries the same prefix, so
the sweep can recognize one by address alone -- and the one address it must
never touch is the tier's persistent account, which is deliberately permanent.

The age cutoff is what keeps a sweep from deleting the account a concurrent run
just minted.
"""

from collections.abc import Sequence
from datetime import datetime
from datetime import timezone

from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.minds.deployment_tests.helpers import SupertokensUserRecord
from imbue.minds.deployment_tests.helpers import iter_supertokens_users
from imbue.minds.smoke_tests.primitives import PERSISTENT_ACCOUNT_EMAIL
from imbue.minds.smoke_tests.primitives import SMOKE_EMAIL_PREFIX


@pure
def is_sweepable_smoke_email(email: str) -> bool:
    """Whether an address is a smoke account a sweep may delete.

    The tier's persistent account carries the prefix too and must survive every
    sweep: it is created once per tier by an operator, and the credentials that
    every smoke run signs in with are its own.
    """
    normalized = email.strip().lower()
    return normalized.startswith(SMOKE_EMAIL_PREFIX) and normalized != PERSISTENT_ACCOUNT_EMAIL


@pure
def select_stale_smoke_users(
    users: Sequence[SupertokensUserRecord], *, cutoff_ms: float
) -> list[tuple[NonEmptyStr, NonEmptyStr]]:
    """The ``(user_id, email)`` of every sweepable smoke account created before ``cutoff_ms``."""
    stale: list[tuple[NonEmptyStr, NonEmptyStr]] = []
    for user in users:
        if user.time_joined_ms > cutoff_ms:
            continue
        sweepable_emails = [email for email in user.emails if is_sweepable_smoke_email(email)]
        if not sweepable_emails:
            continue
        stale.append((user.user_id, NonEmptyStr(sweepable_emails[0])))
    return stale


def find_stale_smoke_users(
    *, connection_uri: SecretStr, api_key: SecretStr, max_age_seconds: int
) -> list[tuple[NonEmptyStr, NonEmptyStr]]:
    """Every smoke account on the tier older than ``max_age_seconds``, as ``(user_id, email)``.

    Finds them rather than deleting them: an account still holding a pool row
    has to have it released first, and the deletion itself goes through
    ``scripts/delete_accounts.py`` so one cascade serves every caller.
    """
    cutoff_ms = (datetime.now(timezone.utc).timestamp() - max_age_seconds) * 1000
    users = tuple(iter_supertokens_users(connection_uri=connection_uri, api_key=api_key))
    stale = select_stale_smoke_users(users, cutoff_ms=cutoff_ms)
    logger.info("Found {} stale smoke account(s) among {} account(s) on the tier.", len(stale), len(users))
    return stale
