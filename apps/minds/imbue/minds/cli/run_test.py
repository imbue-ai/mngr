"""Unit tests for ``minds run`` helpers (currently the
``_ImbueCloudAuthErrorDisabler`` private class).

The full ``run`` command path is exercised by the end-to-end Electron tests
elsewhere; this file isolates the disabler's behaviour against a real
``MultiAccountSessionStore`` + a real on-disk ``settings.toml``, with the
``mngr forward`` plugin subprocess stubbed out via a fake ``EnvelopeStreamConsumer``.
"""

import threading
import tomllib
from pathlib import Path

import pytest
from pydantic import PrivateAttr

from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import set_imbue_cloud_provider_for_account
from imbue.minds.cli.run import _ImbueCloudAuthErrorDisabler
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore

_ROOT_NAME = "tname"
_EMAIL = "alice@example.com"
_PROVIDER_NAME = "imbue_cloud_alice-example-com"
_USER_ID = "00000000-0000-0000-0000-000000000001"


class _FakeConsumer(EnvelopeStreamConsumer):
    """Records each ``bounce_observe()`` call and unblocks a waiter Event.

    Inherits from the real ``EnvelopeStreamConsumer`` so the disabler's
    ``consumer: EnvelopeStreamConsumer`` field is satisfied without any
    type-system gymnastics.
    """

    _bounce_call_count: int = PrivateAttr(default=0)
    _bounce_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def bounce_observe(self) -> None:
        self._bounce_call_count += 1
        self._bounce_event.set()

    @property
    def bounce_call_count(self) -> int:
        return self._bounce_call_count

    @property
    def bounce_event(self) -> threading.Event:
        return self._bounce_event


def _make_fake_consumer() -> _FakeConsumer:
    return _FakeConsumer(resolver=MngrCliBackendResolver())


def _seed_settings_toml(tmp_path: Path) -> Path:
    """Create a minimal mngr profile dir with a per-account provider entry.

    Returns the path to the resulting ``settings.toml``. Mirrors
    ``apps/minds/imbue/minds/bootstrap_test.py::_stub_mngr_host_dir`` shape.
    Relies on ``isolate_home(tmp_path, ...)`` from ``cli/conftest.py`` already
    having set ``$HOME`` to ``tmp_path`` so ``mngr_host_dir_for`` resolves
    under the per-test tmp tree.
    """
    mngr_host_dir = mngr_host_dir_for(_ROOT_NAME)
    mngr_host_dir.mkdir(parents=True, exist_ok=True)
    profile_id = "testprofile"
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    settings_dir = mngr_host_dir / "profiles" / profile_id
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.toml"
    set_imbue_cloud_provider_for_account(_EMAIL, root_name=_ROOT_NAME)
    return settings_path


@pytest.fixture
def session_store_with_alice(tmp_path: Path) -> MultiAccountSessionStore:
    store = MultiAccountSessionStore(data_dir=tmp_path / ".minds")
    store.add_or_update_session(user_id=_USER_ID, email=_EMAIL)
    return store


def test_auth_error_for_known_account_disables_provider_and_bounces_observe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_store_with_alice: MultiAccountSessionStore,
) -> None:
    monkeypatch.setenv("MINDS_ROOT_NAME", _ROOT_NAME)
    settings_path = _seed_settings_toml(tmp_path)
    consumer = _make_fake_consumer()

    disabler = _ImbueCloudAuthErrorDisabler(consumer=consumer, session_store=session_store_with_alice)
    disabler(_PROVIDER_NAME, "ImbueCloudAuthError", "token theft detected")

    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"][_PROVIDER_NAME]["is_enabled"] is False

    assert consumer.bounce_event.wait(timeout=2.0), "bounce_observe was never invoked on the consumer"
    assert consumer.bounce_call_count == 1


def test_auth_error_for_unknown_provider_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_store_with_alice: MultiAccountSessionStore,
) -> None:
    monkeypatch.setenv("MINDS_ROOT_NAME", _ROOT_NAME)
    settings_path = _seed_settings_toml(tmp_path)
    consumer = _make_fake_consumer()

    disabler = _ImbueCloudAuthErrorDisabler(consumer=consumer, session_store=session_store_with_alice)
    disabler("imbue_cloud_someone-not-signed-in", "ImbueCloudAuthError", "token theft detected")

    # Settings file untouched; bounce_observe never called.
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"][_PROVIDER_NAME].get("is_enabled") in (True, None)
    assert consumer.bounce_call_count == 0


def test_non_auth_error_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_store_with_alice: MultiAccountSessionStore,
) -> None:
    """A ``VpsApiError`` (or any non-``ImbueCloudAuthError``) must not flip
    ``is_enabled`` -- transient connector hiccups should self-recover.
    """
    monkeypatch.setenv("MINDS_ROOT_NAME", _ROOT_NAME)
    settings_path = _seed_settings_toml(tmp_path)
    consumer = _make_fake_consumer()

    disabler = _ImbueCloudAuthErrorDisabler(consumer=consumer, session_store=session_store_with_alice)
    disabler(_PROVIDER_NAME, "VpsApiError", "VPS API error 502")

    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"][_PROVIDER_NAME].get("is_enabled") in (True, None)
    assert consumer.bounce_call_count == 0


def test_already_disabled_provider_does_not_re_bounce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_store_with_alice: MultiAccountSessionStore,
) -> None:
    """Idempotency: a second auth error after the provider is already
    disabled is a no-op (no toml rewrite, no observe bounce). Otherwise
    every poll cycle of the dead session would re-bounce.
    """
    monkeypatch.setenv("MINDS_ROOT_NAME", _ROOT_NAME)
    _seed_settings_toml(tmp_path)
    consumer = _make_fake_consumer()
    disabler = _ImbueCloudAuthErrorDisabler(consumer=consumer, session_store=session_store_with_alice)

    # First call disables + bounces.
    disabler(_PROVIDER_NAME, "ImbueCloudAuthError", "token theft detected")
    assert consumer.bounce_event.wait(timeout=2.0)
    consumer.bounce_event.clear()
    first_count = consumer.bounce_call_count

    # Second call: settings already say is_enabled=false, so
    # disable_imbue_cloud_provider_for_account returns False and the
    # disabler skips the bounce.
    disabler(_PROVIDER_NAME, "ImbueCloudAuthError", "token theft detected")
    assert consumer.bounce_call_count == first_count
