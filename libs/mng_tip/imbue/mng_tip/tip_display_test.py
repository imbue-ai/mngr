from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from imbue.mng_tip.invocation_logger import get_tip_data_dir
from imbue.mng_tip.tip_display import TIP_ELIGIBLE_COMMANDS
from imbue.mng_tip.tip_display import maybe_display_tip


class TestTipEligibleCommands:
    def test_create_is_eligible(self) -> None:
        assert "create" in TIP_ELIGIBLE_COMMANDS

    def test_connect_is_eligible(self) -> None:
        assert "connect" in TIP_ELIGIBLE_COMMANDS

    def test_start_is_eligible(self) -> None:
        assert "start" in TIP_ELIGIBLE_COMMANDS

    def test_list_is_not_eligible(self) -> None:
        assert "list" not in TIP_ELIGIBLE_COMMANDS


class TestMaybeDisplayTip:
    def test_no_op_for_ineligible_command(self, temp_host_dir: Path) -> None:
        stderr = StringIO()
        with patch("sys.stderr", stderr):
            maybe_display_tip("list")
        assert stderr.getvalue() == ""

    @patch("imbue.mng_tip.tip_display._kick_off_async_tip_generation")
    def test_displays_tip_when_next_tip_exists(self, mock_gen: Any, temp_host_dir: Path) -> None:
        tip_dir = get_tip_data_dir()
        tip_dir.mkdir(parents=True, exist_ok=True)
        (tip_dir / "next_tip.txt").write_text("Try `mng pair` for file sync")

        stderr = StringIO()
        stderr.isatty = lambda: False  # type: ignore[assignment]
        with patch("sys.stderr", stderr):
            maybe_display_tip("create")

        assert "mng pair" in stderr.getvalue()
        assert "tip:" in stderr.getvalue()

    @patch("imbue.mng_tip.tip_display._kick_off_async_tip_generation")
    def test_deletes_next_tip_after_display(self, mock_gen: Any, temp_host_dir: Path) -> None:
        tip_dir = get_tip_data_dir()
        tip_dir.mkdir(parents=True, exist_ok=True)
        next_tip_path = tip_dir / "next_tip.txt"
        next_tip_path.write_text("some tip")

        with patch("sys.stderr", StringIO()):
            maybe_display_tip("create")

        assert not next_tip_path.exists()

    @patch("imbue.mng_tip.tip_display._kick_off_async_tip_generation")
    def test_kicks_off_generation_when_tip_exists(self, mock_gen: Any, temp_host_dir: Path) -> None:
        tip_dir = get_tip_data_dir()
        tip_dir.mkdir(parents=True, exist_ok=True)
        (tip_dir / "next_tip.txt").write_text("a tip")

        with patch("sys.stderr", StringIO()):
            maybe_display_tip("connect")

        mock_gen.assert_called_once()

    @patch("imbue.mng_tip.tip_display._kick_off_async_tip_generation")
    def test_kicks_off_generation_when_no_tip(self, mock_gen: Any, temp_host_dir: Path) -> None:
        maybe_display_tip("create")
        mock_gen.assert_called_once()

    @patch("imbue.mng_tip.tip_display._kick_off_async_tip_generation")
    def test_no_display_when_tip_file_empty(self, mock_gen: Any, temp_host_dir: Path) -> None:
        tip_dir = get_tip_data_dir()
        tip_dir.mkdir(parents=True, exist_ok=True)
        (tip_dir / "next_tip.txt").write_text("")

        stderr = StringIO()
        with patch("sys.stderr", stderr):
            maybe_display_tip("start")

        assert stderr.getvalue() == ""

    @patch("imbue.mng_tip.tip_display._kick_off_async_tip_generation")
    def test_no_display_when_tip_file_whitespace_only(self, mock_gen: Any, temp_host_dir: Path) -> None:
        tip_dir = get_tip_data_dir()
        tip_dir.mkdir(parents=True, exist_ok=True)
        (tip_dir / "next_tip.txt").write_text("   \n  ")

        stderr = StringIO()
        with patch("sys.stderr", stderr):
            maybe_display_tip("start")

        assert stderr.getvalue() == ""
