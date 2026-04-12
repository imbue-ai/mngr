"""Tests for sharing_proxy module."""

from pathlib import Path

import pytest

from imbue.minds_workspace_server.sharing_proxy import SharingProxyError
from imbue.minds_workspace_server.sharing_proxy import SharingStatus
from imbue.minds_workspace_server.sharing_proxy import _read_minds_api_url
from imbue.minds_workspace_server.sharing_proxy import _read_tunnel_token


def test_read_minds_api_url_missing_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    with pytest.raises(SharingProxyError, match="MNGR_AGENT_STATE_DIR"):
        _read_minds_api_url()


def test_read_minds_api_url_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    with pytest.raises(SharingProxyError, match="not found"):
        _read_minds_api_url()


def test_read_minds_api_url_empty_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url_file = tmp_path / "minds_api_url"
    url_file.write_text("")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    with pytest.raises(SharingProxyError, match="empty"):
        _read_minds_api_url()


def test_read_minds_api_url_valid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url_file = tmp_path / "minds_api_url"
    url_file.write_text("http://127.0.0.1:8420\n")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    assert _read_minds_api_url() == "http://127.0.0.1:8420"


def test_read_tunnel_token_missing_work_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    assert _read_tunnel_token() is None


def test_read_tunnel_token_missing_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    assert _read_tunnel_token() is None


def test_read_tunnel_token_no_token_in_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "runtime"
    secrets_dir.mkdir()
    (secrets_dir / "secrets").write_text("export OTHER_VAR=something\n")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    assert _read_tunnel_token() is None


def test_read_tunnel_token_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "runtime"
    secrets_dir.mkdir()
    (secrets_dir / "secrets").write_text("export CLOUDFLARE_TUNNEL_TOKEN=abc123\n")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    assert _read_tunnel_token() == "abc123"


def test_sharing_status_enabled_with_url() -> None:
    status = SharingStatus(enabled=True, url="https://web.example.com")
    assert status.enabled is True
    assert status.url == "https://web.example.com"


def test_sharing_status_disabled() -> None:
    status = SharingStatus(enabled=False)
    assert status.enabled is False
    assert status.url is None


def test_get_sharing_status_missing_forwarding_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from imbue.minds_workspace_server.sharing_proxy import get_sharing_status

    monkeypatch.delenv("CLOUDFLARE_FORWARDING_URL", raising=False)
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", "/nonexistent")
    with pytest.raises(SharingProxyError):
        get_sharing_status("web")


def test_enable_sharing_missing_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from imbue.minds_workspace_server.sharing_proxy import enable_sharing

    api_url_file = tmp_path / "minds_api_url"
    api_url_file.write_text("http://127.0.0.1:8420")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    monkeypatch.delenv("MINDS_API_KEY", raising=False)
    with pytest.raises(SharingProxyError, match="MINDS_API_KEY"):
        enable_sharing("web")


def test_disable_sharing_missing_agent_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from imbue.minds_workspace_server.sharing_proxy import disable_sharing

    api_url_file = tmp_path / "minds_api_url"
    api_url_file.write_text("http://127.0.0.1:8420")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    monkeypatch.setenv("MINDS_API_KEY", "test-key")
    with pytest.raises(SharingProxyError, match="MNGR_AGENT_ID"):
        disable_sharing("web")
