"""Shared non-fixture test helpers for desktop_client tests."""

import os
import subprocess
import time
from pathlib import Path

from itsdangerous import TimestampSigner
from itsdangerous import URLSafeTimedSerializer

from imbue.minds.desktop_client.restic_cli import _get_restic_binary
from imbue.minds.primitives import CookieSigningKey

# Mirror cookie_manager's private salt/payload so a minted cookie is one the
# production verifier accepts apart from its age. Callers guard this mirror
# against drift by asserting an ``age_seconds=0`` cookie still verifies True.
_MINDS_SESSION_SALT = "minds-auth"
_MINDS_SESSION_PAYLOAD = "authenticated"


def make_backdated_session_cookie(signing_key: CookieSigningKey, age_seconds: int) -> str:
    """Mint a minds session cookie whose signature timestamp is ``age_seconds`` in the past.

    Used to exercise session expiry deterministically without mocking the clock:
    ``age_seconds`` beyond the 30-day max age yields a cookie the verifier rejects
    as expired.
    """

    class _BackdatedTimestampSigner(TimestampSigner):
        def get_timestamp(self) -> int:
            return int(time.time()) - age_seconds

    class _BackdatedSerializer(URLSafeTimedSerializer):
        default_signer = _BackdatedTimestampSigner

    serializer = _BackdatedSerializer(secret_key=signing_key.get_secret_value())
    return serializer.dumps(_MINDS_SESSION_PAYLOAD, salt=_MINDS_SESSION_SALT)


def restic_backup_a_file(repository: str, password: str, source: Path) -> None:
    """Create one snapshot in ``repository`` from ``source`` using plain restic."""
    env = dict(os.environ)
    env.update({"RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD": password})
    result = subprocess.run(
        [_get_restic_binary(), "backup", str(source)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120.0,
    )
    assert result.returncode == 0, result.stderr
