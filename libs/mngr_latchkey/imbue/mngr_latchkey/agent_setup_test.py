"""Unit tests for :mod:`imbue.mngr_latchkey.agent_setup`.

These cover the per-agent latchkey setup helpers without spawning a real
``latchkey gateway`` subprocess. ``Latchkey.ensure_gateway_started`` and
the JWT-mint / password-derivation methods are stubbed via a fake
subclass so we can drive the various success / failure permutations
deterministically.
"""

from collections.abc import Mapping
from pathlib import Path

import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.agent_setup import AgentLatchkeySetup
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_DISABLE_COUNTING
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PASSWORD
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
from imbue.mngr_latchkey.agent_setup import finalize_agent_permissions
from imbue.mngr_latchkey.agent_setup import prepare_agent_latchkey
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import LatchkeyJwtMintError
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import opaque_permissions_dir
from imbue.mngr_latchkey.store import permissions_path_for_agent
from imbue.mngr_latchkey.testing import FakeLatchkey
from imbue.mngr_latchkey.testing import make_full_fake_latchkey


def _full_fake(tmp_path: Path) -> FakeLatchkey:
    return make_full_fake_latchkey(tmp_path)


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


def test_prepare_on_host_gateway_start_failure_propagates(tmp_path: Path) -> None:
    """Gateway-start failures bubble up to the caller; the helper does not swallow them."""
    fake = FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_error=LatchkeyError("boom"),
        password="hunter2",
        jwt="header.payload.signature",
    )
    with pytest.raises(LatchkeyError):
        prepare_agent_latchkey(fake, is_tunneled=False)


def test_prepare_password_derivation_failure_propagates(tmp_path: Path) -> None:
    """Password-derivation failures bubble up to the caller."""
    fake = FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password_error=LatchkeyJwtMintError("nope"),
        jwt="header.payload.signature",
    )
    with pytest.raises(LatchkeyJwtMintError):
        prepare_agent_latchkey(fake, is_tunneled=True)


def test_prepare_jwt_mint_failure_propagates(tmp_path: Path) -> None:
    """JWT-mint failures bubble up to the caller.

    The opaque permissions file may or may not have been materialized
    at the point the exception fires; we don't make any guarantee
    about cleanup -- the caller can either retry the whole prepare
    (which writes a fresh opaque path) or accept the orphan file. The
    files are tiny and live under the user's own latchkey directory,
    so leaking one occasionally is not a concern.
    """
    fake = FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password="hunter2",
        jwt_error=LatchkeyJwtMintError("nope"),
    )
    with pytest.raises(LatchkeyJwtMintError):
        prepare_agent_latchkey(fake, is_tunneled=True)


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


def test_finalize_propagates_link_errors(tmp_path: Path) -> None:
    """Linking failures bubble up to the caller; the helper does not swallow them.

    Callers (e.g. minds) decide whether to fail agent creation or just
    surface a warning; the plugin's job is just to report what happened.
    """
    fake = _full_fake(tmp_path)
    # Pass an opaque path the helper cannot operate on (it doesn't
    # exist), forcing ``link_opaque_permissions_to_agent`` to raise.
    missing_path = tmp_path / "definitely-not-there.json"
    with pytest.raises(LatchkeyStoreError):
        finalize_agent_permissions(fake, missing_path, AgentId())


# -- AgentLatchkeySetup model -------------------------------------------------


def test_agent_latchkey_setup_default_opaque_path_is_none() -> None:
    setup = AgentLatchkeySetup(env={})
    assert setup.opaque_permissions_path is None
    assert isinstance(setup.env, Mapping)
