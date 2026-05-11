"""Unit tests for :mod:`imbue.mngr_latchkey.agent_setup`.

These cover the per-agent latchkey setup helpers without spawning a real
``latchkey gateway`` subprocess. ``Latchkey.ensure_gateway_started`` and
the JWT-mint / password-derivation methods are stubbed via a fake
subclass so we can drive the various success / failure permutations
deterministically.
"""

from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import PrivateAttr

from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr_latchkey.agent_setup import AgentLatchkeySetup
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_DISABLE_COUNTING
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PASSWORD
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
from imbue.mngr_latchkey.agent_setup import finalize_agent_permissions
from imbue.mngr_latchkey.agent_setup import prepare_agent_latchkey
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import LatchkeyJwtMintError
from imbue.mngr_latchkey.store import LatchkeyGatewayInfo
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import host_id_path_for_host
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import read_stored_host_id
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.store import write_stored_host_id

_HOST_NAME = HostName("alpha-host")


class _FakeLatchkey(Latchkey):
    """Test double for :class:`Latchkey` that never spawns subprocesses.

    Each method either returns the configured fake value or raises
    the configured fake error so individual tests can assert the
    degradation semantics of :func:`prepare_agent_latchkey`.
    """

    _gateway_url: str | None = PrivateAttr(default=None)
    _gateway_error: BaseException | None = PrivateAttr(default=None)
    _password: str | None = PrivateAttr(default=None)
    _password_error: BaseException | None = PrivateAttr(default=None)
    _jwt: str | None = PrivateAttr(default=None)
    _jwt_error: BaseException | None = PrivateAttr(default=None)

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

    def ensure_gateway_started(self) -> LatchkeyGatewayInfo:
        if self._gateway_error is not None:
            raise self._gateway_error
        if self._gateway_url is None:
            raise LatchkeyError("test fake: configure gateway_url")
        parts = urlsplit(self._gateway_url)
        if parts.hostname is None or parts.port is None:
            raise LatchkeyError(f"unparseable url: {self._gateway_url}")
        return LatchkeyGatewayInfo(
            host=parts.hostname,
            port=parts.port,
            pid=42,
            started_at=datetime.now(timezone.utc),
        )

    def derive_gateway_password(self) -> str:
        if self._password_error is not None:
            raise self._password_error
        if self._password is None:
            raise LatchkeyJwtMintError("test fake: configure password")
        return self._password

    def create_permissions_override_jwt(self, permissions_path: Path) -> str:
        if self._jwt_error is not None:
            raise self._jwt_error
        if self._jwt is None:
            raise LatchkeyJwtMintError("test fake: configure jwt")
        return self._jwt


def _full_fake(tmp_path: Path) -> _FakeLatchkey:
    fake = _FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password="hunter2",
        jwt="header.payload.signature",
    )
    return fake


# -- prepare_agent_latchkey ---------------------------------------------------


def test_prepare_no_latchkey_tunneled_returns_constant_url(tmp_path: Path) -> None:
    """``latchkey=None`` with ``is_tunneled=True`` still injects the constant URL.

    The constant agent-side URL is meaningful even without a configured
    latchkey wrapper -- tests and non-password-protected gateways can
    still receive traffic at that URL.
    """
    setup = prepare_agent_latchkey(None, _HOST_NAME, is_tunneled=True)
    assert setup.env[ENV_LATCHKEY_GATEWAY] == f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    assert setup.env[ENV_LATCHKEY_DISABLE_COUNTING] == "1"
    assert ENV_LATCHKEY_GATEWAY_PASSWORD not in setup.env
    assert ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE not in setup.env


def test_prepare_no_latchkey_on_host_returns_empty(tmp_path: Path) -> None:
    """``latchkey=None`` with ``is_tunneled=False`` cannot produce a URL.

    On-host agents need the gateway's live port; without a Latchkey
    wrapper there is nothing to query.
    """
    setup = prepare_agent_latchkey(None, _HOST_NAME, is_tunneled=False)
    assert setup.env == {}


def test_prepare_full_wiring_tunneled(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, _HOST_NAME, is_tunneled=True)
    assert setup.env[ENV_LATCHKEY_GATEWAY] == f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PASSWORD] == "hunter2"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] == "header.payload.signature"
    assert setup.env[ENV_LATCHKEY_DISABLE_COUNTING] == "1"
    # The per-host permissions file was materialized with deny-all baseline.
    permissions_path = permissions_path_for_host(fake.plugin_data_dir, _HOST_NAME)
    assert permissions_path.is_file()
    assert load_permissions(permissions_path).rules == ()


def test_prepare_full_wiring_on_host_uses_live_port(tmp_path: Path) -> None:
    """On-host (DEV) agents get the gateway's live host:port pair."""
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, _HOST_NAME, is_tunneled=False)
    assert setup.env[ENV_LATCHKEY_GATEWAY] == "http://127.0.0.1:55555"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PASSWORD] == "hunter2"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] == "header.payload.signature"


def test_prepare_preserves_existing_permissions_file(tmp_path: Path) -> None:
    """A pre-existing permissions file is *not* overwritten by prepare.

    Re-deploying the same host (same ``host_id``) should keep prior
    grants intact -- :func:`finalize_agent_permissions` is the only
    place that clears them, and only when the recorded ``host-id``
    doesn't match.
    """
    fake = _full_fake(tmp_path)
    permissions_path = permissions_path_for_host(fake.plugin_data_dir, _HOST_NAME)
    save_permissions(
        permissions_path,
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
    )

    prepare_agent_latchkey(fake, _HOST_NAME, is_tunneled=True)

    assert load_permissions(permissions_path).rules == ({"slack-api": ["slack-read-all"]},)


def test_prepare_on_host_gateway_start_failure_propagates(tmp_path: Path) -> None:
    """Gateway-start failures bubble up to the caller; the helper does not swallow them."""
    fake = _FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_error=LatchkeyError("boom"),
        password="hunter2",
        jwt="header.payload.signature",
    )
    with pytest.raises(LatchkeyError):
        prepare_agent_latchkey(fake, _HOST_NAME, is_tunneled=False)


def test_prepare_password_derivation_failure_propagates(tmp_path: Path) -> None:
    """Password-derivation failures bubble up to the caller."""
    fake = _FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password_error=LatchkeyJwtMintError("nope"),
        jwt="header.payload.signature",
    )
    with pytest.raises(LatchkeyJwtMintError):
        prepare_agent_latchkey(fake, _HOST_NAME, is_tunneled=True)


def test_prepare_jwt_mint_failure_propagates(tmp_path: Path) -> None:
    """JWT-mint failures bubble up to the caller."""
    fake = _FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password="hunter2",
        jwt_error=LatchkeyJwtMintError("nope"),
    )
    with pytest.raises(LatchkeyJwtMintError):
        prepare_agent_latchkey(fake, _HOST_NAME, is_tunneled=True)


# -- finalize_agent_permissions ----------------------------------------------


def test_finalize_records_host_id_on_first_call(tmp_path: Path) -> None:
    """First finalize for a host writes the canonical host-id alongside the permissions file."""
    fake = _full_fake(tmp_path)
    prepare_agent_latchkey(fake, _HOST_NAME, is_tunneled=True)
    host_id = HostId()

    finalize_agent_permissions(fake, _HOST_NAME, host_id)

    assert read_stored_host_id(fake.plugin_data_dir, _HOST_NAME) == host_id


def test_finalize_is_a_noop_when_host_id_matches(tmp_path: Path) -> None:
    """Re-running with the same host_id preserves prior grants."""
    fake = _full_fake(tmp_path)
    host_id = HostId()
    permissions_path = permissions_path_for_host(fake.plugin_data_dir, _HOST_NAME)
    save_permissions(
        permissions_path,
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
    )
    write_stored_host_id(fake.plugin_data_dir, _HOST_NAME, host_id)

    finalize_agent_permissions(fake, _HOST_NAME, host_id)

    assert load_permissions(permissions_path).rules == ({"slack-api": ["slack-read-all"]},)
    assert read_stored_host_id(fake.plugin_data_dir, _HOST_NAME) == host_id


def test_finalize_clears_permissions_on_host_id_mismatch(tmp_path: Path) -> None:
    """A different host_id means the host was recreated; prior grants are stale."""
    fake = _full_fake(tmp_path)
    permissions_path = permissions_path_for_host(fake.plugin_data_dir, _HOST_NAME)
    save_permissions(
        permissions_path,
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
    )
    old_id = HostId()
    new_id = HostId()
    write_stored_host_id(fake.plugin_data_dir, _HOST_NAME, old_id)

    finalize_agent_permissions(fake, _HOST_NAME, new_id)

    assert load_permissions(permissions_path).rules == ()
    assert read_stored_host_id(fake.plugin_data_dir, _HOST_NAME) == new_id


def test_finalize_clears_permissions_when_no_host_id_recorded(tmp_path: Path) -> None:
    """Absent host-id file is treated like a mismatch.

    The permissions file may contain rules from a previous tenant of the
    same host name (e.g. left over from before the host-id check was
    introduced) -- clear them defensively.
    """
    fake = _full_fake(tmp_path)
    permissions_path = permissions_path_for_host(fake.plugin_data_dir, _HOST_NAME)
    save_permissions(
        permissions_path,
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
    )
    assert not host_id_path_for_host(fake.plugin_data_dir, _HOST_NAME).is_file()
    new_id = HostId()

    finalize_agent_permissions(fake, _HOST_NAME, new_id)

    assert load_permissions(permissions_path).rules == ()
    assert read_stored_host_id(fake.plugin_data_dir, _HOST_NAME) == new_id


# -- AgentLatchkeySetup model -------------------------------------------------


def test_agent_latchkey_setup_env_is_a_mapping() -> None:
    setup = AgentLatchkeySetup(env={})
    assert isinstance(setup.env, Mapping)
