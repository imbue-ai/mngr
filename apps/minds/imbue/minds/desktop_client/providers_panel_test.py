"""Unit tests for the providers-panel endpoint and payload builder added in this branch.

Covers:
- POST ``/api/providers/{provider_name}/toggle`` (``_handle_provider_toggle``):
  authn, body validation, and the happy-path settings.toml write.
- ``_build_providers_state_payload``: combines resolver-tracked providers,
  errored providers, and disabled-on-disk providers into the SSE payload.

The toggle endpoint also sends ``SIGHUP`` to the ``mngr forward`` plugin via
``EnvelopeStreamConsumer.bounce_observe``. Tests deliberately do not wire a
consumer (``create_desktop_client`` defaults the slot to ``None``); the
handler's ``if consumer is not None`` guard makes the bounce a no-op in that
case. The bounce side-effect itself is exercised by the forward_cli unit
tests; this file focuses on the new routing/validation/serialization logic.
"""

import tomllib
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.desktop_client.app import _build_providers_state_payload
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName

_ROOT_NAME = "minds-dev-tname"


def _stub_mngr_host_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root_name: str) -> Path:
    """Mirror of ``bootstrap_test._stub_mngr_host_dir``.

    Redirect ``Path.home()`` to ``tmp_path`` and seed an empty mngr profile
    so ``set_provider_is_enabled`` has a settings.toml path to write to.
    Returns the active settings.toml path (which may not exist yet on entry).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    mngr_host_dir = mngr_host_dir_for(root_name)
    mngr_host_dir.mkdir(parents=True, exist_ok=True)
    profile_id = "testprofile"
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    settings_dir = mngr_host_dir / "profiles" / profile_id
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.toml"


def _make_test_client(tmp_path: Path) -> tuple[TestClient, FileAuthStore]:
    """Build a desktop client backed by a real MngrCliBackendResolver + on-disk authn."""
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    resolver = MngrCliBackendResolver()
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
    )
    client = TestClient(app, base_url="http://localhost")
    return client, auth_store


def _authenticate(client: TestClient, auth_store: FileAuthStore) -> None:
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value, path="/")


# -- _handle_provider_toggle -----------------------------------------------


def test_provider_toggle_requires_authentication(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unauthenticated POST is rejected with 403 and does not touch settings."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _ROOT_NAME)
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    client, _ = _make_test_client(tmp_path)

    response = client.post("/api/providers/modal/toggle", json={"is_enabled": False})

    assert response.status_code == 403
    assert not settings_path.exists()


def test_provider_toggle_returns_400_on_non_json_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-JSON body is rejected with 400 and settings are not modified."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _ROOT_NAME)
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    client, auth_store = _make_test_client(tmp_path)
    _authenticate(client, auth_store)

    response = client.post(
        "/api/providers/modal/toggle",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert not settings_path.exists()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"is_enabled": "yes"},
        {"is_enabled": 1},
        {"is_enabled": None},
    ],
)
def test_provider_toggle_returns_400_when_is_enabled_missing_or_wrong_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict[str, object]
) -> None:
    """``is_enabled`` must be present and a bool; otherwise 400 and no settings write."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _ROOT_NAME)
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    client, auth_store = _make_test_client(tmp_path)
    _authenticate(client, auth_store)

    response = client.post("/api/providers/modal/toggle", json=body)

    assert response.status_code == 400
    assert not settings_path.exists()


def test_provider_toggle_writes_settings_and_returns_changed_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Happy path: flips ``is_enabled`` in the toml file and returns ``changed=True``."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _ROOT_NAME)
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    client, auth_store = _make_test_client(tmp_path)
    _authenticate(client, auth_store)

    response = client.post("/api/providers/modal/toggle", json={"is_enabled": False})

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"provider_name": "modal", "is_enabled": False, "changed": True}
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["modal"] == {"is_enabled": False}


def test_provider_toggle_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A second identical call returns ``changed=False`` without rewriting the file."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _ROOT_NAME)
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    client, auth_store = _make_test_client(tmp_path)
    _authenticate(client, auth_store)

    first = client.post("/api/providers/modal/toggle", json={"is_enabled": False})
    assert first.status_code == 200
    assert first.json()["changed"] is True
    mtime_after_first = settings_path.stat().st_mtime_ns

    second = client.post("/api/providers/modal/toggle", json={"is_enabled": False})
    assert second.status_code == 200
    assert second.json()["changed"] is False
    # File untouched
    assert settings_path.stat().st_mtime_ns == mtime_after_first


# -- _build_providers_state_payload ----------------------------------------


def _make_discovered_provider(name: str, backend: str = "docker") -> object:
    return make_discovered_provider(
        ProviderInstanceName(name),
        ProviderInstanceConfig(backend=ProviderBackendName(backend), is_enabled=True),
    )


def test_build_providers_state_payload_returns_empty_for_non_mngr_resolver() -> None:
    """A non-``MngrCliBackendResolver`` resolver yields an empty payload (defensive default)."""
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    payload = _build_providers_state_payload(resolver)

    assert payload == {"providers": [], "last_event_at": None, "last_full_snapshot_at": None}


def test_build_providers_state_payload_hides_local_provider() -> None:
    """The ``local`` provider is filtered out of the panel even when reported as healthy."""
    resolver = MngrCliBackendResolver()
    now = datetime.now(timezone.utc)
    resolver.update_providers(
        providers=(_make_discovered_provider("local"),),
        error_by_provider_name={},
        last_full_snapshot_at=now,
    )

    payload = _build_providers_state_payload(resolver)

    assert payload["providers"] == []
    assert payload["last_full_snapshot_at"] == now.isoformat()
    assert payload["last_event_at"] == now.isoformat()


def test_build_providers_state_payload_combines_ok_error_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Healthy + errored + disabled providers all appear, sorted alphabetically."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _ROOT_NAME)
    settings_path = _stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    # Seed a [providers.docker] is_enabled=false block on disk so
    # list_disabled_provider_names() surfaces it.
    settings_path.write_text("[providers.docker]\nis_enabled = false\n")

    errored_name = ProviderInstanceName("modal")
    resolver = MngrCliBackendResolver()
    resolver.update_providers(
        providers=(_make_discovered_provider("zzz_last"), _make_discovered_provider("local")),
        error_by_provider_name={
            errored_name: DiscoveryError(
                type_name="ImbueCloudAuthError",
                message="token missing",
                provider_name=errored_name,
            ),
        },
        last_full_snapshot_at=datetime.now(timezone.utc),
    )

    payload = _build_providers_state_payload(resolver)
    names = [entry["name"] for entry in payload["providers"]]

    # `local` is hidden; the rest are sorted alphabetically across all categories.
    assert names == ["docker", "modal", "zzz_last"]
    by_name = {entry["name"]: entry for entry in payload["providers"]}
    assert by_name["docker"]["status"] == "disabled"
    assert by_name["docker"]["is_enabled"] is False
    assert by_name["modal"]["status"] == "error"
    assert by_name["modal"]["error_type"] == "ImbueCloudAuthError"
    assert by_name["modal"]["error_message"] == "token missing"
    assert by_name["zzz_last"]["status"] == "ok"
    assert by_name["zzz_last"]["backend"] == "docker"
