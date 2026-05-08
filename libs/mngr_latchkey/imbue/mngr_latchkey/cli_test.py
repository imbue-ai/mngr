"""Unit tests for the ``mngr latchkey`` CLI surface.

The ``ensure-gateway`` happy path spawns a real ``latchkey gateway``
subprocess, so the integration coverage for it lives in
``core_test.py`` (which already exercises ``Latchkey.ensure_gateway_started``
via a fake binary). Here we check the smaller CLI-shaped concerns that
don't need a subprocess: data-dir resolution.
"""

from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_latchkey.cli import _resolve_data_dir
from imbue.mngr_latchkey.cli import _resolve_latchkey_directory


def test_resolve_data_dir_defaults_to_profile_subdir(temp_mngr_ctx: MngrContext) -> None:
    """Without an override the data dir lives next to the profile."""
    resolved = _resolve_data_dir(temp_mngr_ctx, override=None)
    assert resolved == temp_mngr_ctx.profile_dir / "latchkey"


def test_resolve_data_dir_honors_override(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    custom = tmp_path / "custom-latchkey"
    resolved = _resolve_data_dir(temp_mngr_ctx, override=str(custom))
    assert resolved == custom


def test_resolve_latchkey_directory_defaults_under_data_dir(tmp_path: Path) -> None:
    """Defaults to ``<data_dir>/latchkey-credentials`` -- separated from plugin metadata."""
    resolved = _resolve_latchkey_directory(tmp_path / "data", override=None)
    assert resolved == tmp_path / "data" / "latchkey-credentials"


def test_resolve_latchkey_directory_honors_override(tmp_path: Path) -> None:
    custom = tmp_path / "share"
    resolved = _resolve_latchkey_directory(tmp_path / "data", override=str(custom))
    assert resolved == custom
