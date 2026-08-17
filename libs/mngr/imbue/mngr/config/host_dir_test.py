"""Unit tests for host-dir resolution helpers."""

from pathlib import Path

import pytest

from imbue.mngr.config.host_dir import deploy_dest_host_dir
from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr.config.host_dir import read_root_name


def test_read_root_name_defaults_to_mngr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_ROOT_NAME", raising=False)
    assert read_root_name() == "mngr"


def test_read_root_name_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_ROOT_NAME", "minds")
    assert read_root_name() == "minds"


def test_deploy_dest_host_dir_is_tilde_and_unexpanded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deploy destination root is a tilde path resolved against the REMOTE $HOME,
    so it must not be expanded against the local home."""
    monkeypatch.delenv("MNGR_ROOT_NAME", raising=False)
    dest = deploy_dest_host_dir()
    assert dest == Path("~/.mngr")
    assert str(dest).startswith("~")


def test_deploy_dest_host_dir_uses_root_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_ROOT_NAME", "minds")
    assert deploy_dest_host_dir() == Path("~/.minds")


def test_read_default_host_dir_prefers_mngr_host_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_ROOT_NAME", "minds")
    monkeypatch.setenv("MNGR_HOST_DIR", "/tmp/custom-mngr-home")
    assert read_default_host_dir() == Path("/tmp/custom-mngr-home")


def test_read_default_host_dir_falls_back_to_root_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.setenv("MNGR_ROOT_NAME", "minds")
    assert read_default_host_dir() == Path("~/.minds").expanduser()
