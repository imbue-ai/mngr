"""Unit tests for the ``mngr latchkey`` CLI surface.

The ``ensure-gateway`` happy path spawns a real ``latchkey gateway``
subprocess, so the integration coverage for it lives in
``core_test.py`` (which already exercises ``Latchkey.ensure_gateway_started``
via a fake binary). Here we check the smaller CLI-shaped concerns that
don't need a subprocess: data-dir resolution.
"""

from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_latchkey.cli import DEFAULT_LATCHKEY_DIR_NAME
from imbue.mngr_latchkey.cli import _resolve_latchkey_dir


def test_resolve_latchkey_dir_defaults_to_profile_subdir(temp_mngr_ctx: MngrContext) -> None:
    """Without an override the dir lives at ``<profile>/latchkey``."""
    resolved = _resolve_latchkey_dir(temp_mngr_ctx, override=None)
    assert resolved == temp_mngr_ctx.profile_dir / DEFAULT_LATCHKEY_DIR_NAME


def test_resolve_latchkey_dir_honors_override(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    custom = tmp_path / "custom-latchkey"
    resolved = _resolve_latchkey_dir(temp_mngr_ctx, override=str(custom))
    assert resolved == custom
