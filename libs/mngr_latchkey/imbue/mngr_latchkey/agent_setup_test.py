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

from pydantic import PrivateAttr

from imbue.mngr.primitives import AgentId
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
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import opaque_permissions_dir
from imbue.mngr_latchkey.store import permissions_path_for_agent


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
    setup = prepare_agent_latchkey(None, is_tunneled=True)
    assert setup.env[ENV_LATCHKEY_GATEWAY] == f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    assert setup.env[ENV_LATCHKEY_DISABLE_COUNTING] == "1"
    assert ENV_LATCHKEY_GATEWAY_PASSWORD not in setup.env
    assert ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE not in setup.env
    assert setup.opaque_permissions_path is None


def test_prepare_no_latchkey_on_host_returns_empty(tmp_path: Path) -> None:
    """``latchkey=None`` with ``is_tunneled=False`` cannot produce a URL.

    On-host agents need the gateway's live port; without a Latchkey
    wrapper there is nothing to query.
    """
    setup = prepare_agent_latchkey(None, is_tunneled=False)
    assert setup.env == {}
    assert setup.opaque_permissions_path is None


def test_prepare_full_wiring_tunneled(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, is_tunneled=True)
    assert setup.env[ENV_LATCHKEY_GATEWAY] == f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PASSWORD] == "hunter2"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] == "header.payload.signature"
    assert setup.env[ENV_LATCHKEY_DISABLE_COUNTING] == "1"
    assert setup.opaque_permissions_path is not None
    # The opaque path lives under the plugin's data subdir and was
    # materialized with deny-all baseline rules.
    assert setup.opaque_permissions_path.parent == opaque_permissions_dir(fake.plugin_data_dir)
    assert load_permissions(setup.opaque_permissions_path).rules == ()


def test_prepare_full_wiring_on_host_uses_live_port(tmp_path: Path) -> None:
    """On-host (DEV) agents get the gateway's live host:port pair."""
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, is_tunneled=False)
    assert setup.env[ENV_LATCHKEY_GATEWAY] == "http://127.0.0.1:55555"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PASSWORD] == "hunter2"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] == "header.payload.signature"


def test_prepare_on_host_gateway_start_failure_returns_empty(tmp_path: Path) -> None:
    fake = _FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_error=LatchkeyError("boom"),
        password="hunter2",
        jwt="header.payload.signature",
    )
    setup = prepare_agent_latchkey(fake, is_tunneled=False)
    assert setup.env == {}
    assert setup.opaque_permissions_path is None


def test_prepare_password_derivation_failure_skips_password(tmp_path: Path) -> None:
    """A password-derivation failure must not block the rest of the wiring.

    The agent still gets ``LATCHKEY_GATEWAY`` (and the JWT) -- the only
    consequence is that a password-protected gateway will reject this
    agent. That's a clearer failure mode than aborting agent creation.
    """
    fake = _FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password_error=LatchkeyJwtMintError("nope"),
        jwt="header.payload.signature",
    )
    setup = prepare_agent_latchkey(fake, is_tunneled=True)
    assert ENV_LATCHKEY_GATEWAY in setup.env
    assert ENV_LATCHKEY_GATEWAY_PASSWORD not in setup.env
    assert setup.env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] == "header.payload.signature"


def test_prepare_jwt_mint_failure_skips_override_and_cleans_up(tmp_path: Path) -> None:
    fake = _FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password="hunter2",
        jwt_error=LatchkeyJwtMintError("nope"),
    )
    setup = prepare_agent_latchkey(fake, is_tunneled=True)
    assert setup.env[ENV_LATCHKEY_GATEWAY_PASSWORD] == "hunter2"
    assert ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE not in setup.env
    assert setup.opaque_permissions_path is None
    # The orphan opaque file should have been unlinked, not left behind.
    opaque_dir = opaque_permissions_dir(fake.plugin_data_dir)
    assert not opaque_dir.exists() or list(opaque_dir.iterdir()) == []


# -- finalize_agent_permissions ----------------------------------------------


def test_finalize_links_opaque_to_canonical(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, is_tunneled=True)
    assert setup.opaque_permissions_path is not None
    agent_id = AgentId()

    finalize_agent_permissions(fake, setup.opaque_permissions_path, agent_id)

    # The opaque path is now a symlink pointing at the canonical agent path.
    canonical = permissions_path_for_agent(fake.plugin_data_dir, agent_id)
    assert canonical.is_file()
    assert setup.opaque_permissions_path.is_symlink()
    assert setup.opaque_permissions_path.resolve() == canonical.resolve()


def test_finalize_with_none_path_is_a_noop(tmp_path: Path) -> None:
    """``opaque_permissions_path=None`` is what ``prepare_agent_latchkey`` returns on JWT-mint failure."""
    fake = _full_fake(tmp_path)
    finalize_agent_permissions(fake, None, AgentId())
    assert not (fake.plugin_data_dir / "agents").exists()


# -- AgentLatchkeySetup model -------------------------------------------------


def test_agent_latchkey_setup_default_opaque_path_is_none() -> None:
    setup = AgentLatchkeySetup(env={})
    assert setup.opaque_permissions_path is None
    assert isinstance(setup.env, Mapping)
