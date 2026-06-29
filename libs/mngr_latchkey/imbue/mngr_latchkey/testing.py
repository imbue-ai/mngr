"""Test helpers for ``mngr_latchkey`` unit + integration tests.

Per CLAUDE.md, do not create tests for this module itself; the helpers
are exercised through the tests that import them.
"""

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import LATCHKEY_AUTH_OPTION_BROWSER
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import LatchkeyJwtMintError
from imbue.mngr_latchkey.core import LatchkeyServiceInfo


class FakeLatchkey(Latchkey):
    """Test double for :class:`Latchkey` that never spawns subprocesses.

    Each method either returns the configured fake value or raises the
    configured fake error so individual tests can assert the degradation
    semantics of callers that depend on ``Latchkey``.
    """

    _gateway_url: str | None = PrivateAttr(default=None)
    _gateway_error: BaseException | None = PrivateAttr(default=None)
    _password: str | None = PrivateAttr(default=None)
    _password_error: BaseException | None = PrivateAttr(default=None)
    _jwt: str | None = PrivateAttr(default=None)
    _jwt_error: BaseException | None = PrivateAttr(default=None)
    _is_stopped: bool = PrivateAttr(default=False)

    # Auth / services-info doubles. The credential-grant flow now lives in the
    # real ``Latchkey.auth_browser`` (tested against a fake binary in
    # core_test.py); the fake only needs non-spawning stand-ins so it never
    # shells out. ``services_info`` reports a browser-capable MISSING service.
    _service_info: LatchkeyServiceInfo = PrivateAttr(
        default=LatchkeyServiceInfo(
            credential_status=CredentialStatus.MISSING,
            auth_options=frozenset({LATCHKEY_AUTH_OPTION_BROWSER}),
            set_credentials_example=None,
        )
    )

    def configure(
        self,
        *,
        gateway_url: str | None = None,
        gateway_error: BaseException | None = None,
        password: str | None = None,
        password_error: BaseException | None = None,
        jwt: str | None = None,
        jwt_error: BaseException | None = None,
    ) -> None:
        self._gateway_url = gateway_url
        self._gateway_error = gateway_error
        self._password = password
        self._password_error = password_error
        self._jwt = jwt
        self._jwt_error = jwt_error

    def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo:
        del service_name, is_offline
        return self._service_info

    def auth_prepare(self, service_name: str, client_id: str, client_secret: str) -> tuple[bool, str]:
        del service_name, client_id, client_secret
        return (True, "")

    def auth_clear(self, service_name: str) -> tuple[bool, str]:
        del service_name
        return (True, "")

    def auth_browser_login(self, service_name: str) -> tuple[bool, str]:
        del service_name
        return (True, "")

    def auth_browser(self, service_name: str) -> tuple[bool, str]:
        del service_name
        return (True, "")

    def initialize(self) -> None:
        # No-op: the real implementation runs ``latchkey --version`` and
        # reconciles the on-disk gateway record, neither of which we want
        # in unit tests. Subclasses inherit the ``_is_initialized`` private
        # attribute so we mark ourselves initialized for any downstream
        # invariant check.
        self._is_initialized = True

    def start_gateway(self, concurrency_group: ConcurrencyGroup) -> int:
        # The fake never actually spawns; the CG argument is accepted
        # only to mirror the production signature.
        del concurrency_group
        if self._gateway_error is not None:
            raise self._gateway_error
        if self._gateway_url is None:
            raise LatchkeyError("FakeLatchkey: configure gateway_url before calling start_gateway")
        parts = urlsplit(self._gateway_url)
        if parts.hostname is None or parts.port is None:
            raise LatchkeyError(f"FakeLatchkey: unparseable url: {self._gateway_url}")
        return parts.port

    def derive_gateway_password(self) -> str:
        if self._password_error is not None:
            raise self._password_error
        if self._password is None:
            raise LatchkeyJwtMintError("FakeLatchkey: configure password before calling derive_gateway_password")
        return self._password

    def create_permissions_override_jwt(self, permissions_path: Path) -> str:
        del permissions_path
        if self._jwt_error is not None:
            raise self._jwt_error
        if self._jwt is None:
            raise LatchkeyJwtMintError("FakeLatchkey: configure jwt before calling create_permissions_override_jwt")
        return self._jwt

    def stop_gateway(self) -> None:
        # Record the call so tests can verify ``mngr latchkey forward``'s
        # coupled-lifetime shutdown semantics without spawning a real
        # gateway subprocess.
        self._is_stopped = True

    @property
    def is_stopped(self) -> bool:
        return self._is_stopped


def make_full_fake_latchkey(latchkey_directory: Path) -> FakeLatchkey:
    """Return a :class:`FakeLatchkey` with every method's success path pre-configured."""
    fake = FakeLatchkey(latchkey_directory=latchkey_directory)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password="hunter2",
        jwt="header.payload.signature",
    )
    return fake
