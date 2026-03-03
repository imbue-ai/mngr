import subprocess
import sys
from typing import Final

from imbue.mng.utils.terminal import write_dim_stderr
from imbue.mng_tip.invocation_logger import get_tip_data_dir

# Commands where a tip is displayed (commands with a meaningful wait).
TIP_ELIGIBLE_COMMANDS: Final[frozenset[str]] = frozenset({"create", "connect", "start"})


def maybe_display_tip(command_name: str) -> None:
    """Show a tip if one is queued and the command is eligible.

    Reads and deletes next_tip.txt so the same tip is not shown twice.
    Then kicks off background generation for the next tip.

    If next_tip.txt does not exist, kicks off background generation
    without displaying anything.
    """
    if command_name not in TIP_ELIGIBLE_COMMANDS:
        return

    next_tip_path = get_tip_data_dir() / "next_tip.txt"

    if next_tip_path.exists():
        try:
            tip_text = next_tip_path.read_text().strip()
        except OSError:
            tip_text = ""

        if tip_text:
            write_dim_stderr(f"  tip: {tip_text}", stream=sys.stderr)

        try:
            next_tip_path.unlink(missing_ok=True)
        except OSError:
            pass

    _kick_off_async_tip_generation()


def _kick_off_async_tip_generation() -> None:
    """Spawn a detached subprocess to generate the next tip.

    The subprocess runs ``python -m imbue.mng_tip.tip_generator`` with
    the current environment (inheriting MNG_HOST_DIR, etc.).

    Uses start_new_session=True so the child is fully detached and
    does not block the parent mng process on exit.
    """
    try:
        subprocess.Popen(
            [sys.executable, "-m", "imbue.mng_tip.tip_generator"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError):
        pass
