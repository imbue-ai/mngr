"""Where a program an agent names would run from on this computer.

A remote command's first argv element is either a bare name (``rsync``), which
the user's shell would find on their PATH, or a ``~``-rooted or absolute path.
Two places need the same answer: the approval dialog, which shows the user what
the name resolves to before they grant it, and the executor, which runs it.

The PATH used for a bare name is the user's login shell's, not the desktop
process's own. The packaged app inherits a sparse PATH from the window system
(on macOS a few system directories), so a tool the user has in every terminal
would otherwise read as "not found" here. The login shell's PATH is read by
running the user's shell once with ``-lc`` and printing ``$PATH``, cached for a
short while so a dialog and the command that follows it see the same answer
without paying for a shell start each time; when the shell cannot answer, the
desktop's own PATH is used.
"""

import os
import shutil
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# Programs that run whatever they are handed, so a grant for one is a grant for
# any command and the dialog says so: shells, language runtimes, package
# runners, and the wrappers that hand control to another program.
INTERPRETER_PROGRAM_NAMES: Final[frozenset[str]] = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "fish",
        "dash",
        "ksh",
        "csh",
        "tcsh",
        "pwsh",
        "python",
        "python3",
        "node",
        "deno",
        "bun",
        "ruby",
        "perl",
        "php",
        "lua",
        "osascript",
        "uv",
        "uvx",
        "pipx",
        "npx",
        "pnpm",
        "npm",
        "yarn",
        "bunx",
        "make",
        "just",
        "env",
        "sudo",
        "doas",
        "su",
        "nohup",
        "xargs",
        "exec",
        "eval",
        "open",
        "xdg-open",
    }
)

# How long a login shell gets to print its PATH, and how long the answer is reused.
_LOGIN_SHELL_TIMEOUT_SECONDS: Final[float] = 10.0
_LOGIN_SHELL_PATH_TTL_SECONDS: Final[float] = 30.0
_DEFAULT_SHELL: Final[str] = "/bin/sh"


class ResolvedProgram(FrozenModel):
    """What one program name means on this computer right now."""

    program: str = Field(description="The name or path as the agent sent it")
    path: Path | None = Field(description="The file it resolves to, or None when nothing is there (yet)")
    is_interpreter: bool = Field(description="Whether it is a shell, runtime, or runner that can run anything")


@pure
def is_interpreter_program(program: str) -> bool:
    """Whether the program's basename names a shell, runtime, or runner; a trailing version (``python3.12``) counts."""
    basename = os.path.basename(program)
    if basename in INTERPRETER_PROGRAM_NAMES:
        return True
    stem = basename.rstrip("0123456789.")
    return stem in INTERPRETER_PROGRAM_NAMES


@pure
def expand_program_home(program: str, home_dir: Path) -> str:
    """``~/x`` as the user's path; a bare name or an absolute path unchanged."""
    if program.startswith("~/"):
        return str(home_dir / program[2:])
    return program


def resolve_program(program: str, home_dir: Path, path_env: str | None) -> ResolvedProgram:
    """Where ``program`` would run from: a path as itself, a bare name through ``path_env``."""
    expanded = expand_program_home(program, home_dir)
    if "/" in expanded:
        candidate: str | None = expanded if os.path.exists(expanded) else None
    else:
        candidate = shutil.which(expanded, path=path_env)
    return ResolvedProgram(
        program=program,
        path=Path(candidate) if candidate is not None else None,
        is_interpreter=is_interpreter_program(expanded),
    )


class LoginShellPath(MutableModel):
    """The user's login-shell PATH, read on demand and reused for a short while."""

    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Runs the shell that prints the PATH")
    environment: Mapping[str, str] = Field(
        frozen=True, description="The desktop's own environment: names the shell, and is the fallback PATH"
    )
    ttl_seconds: float = Field(
        default=_LOGIN_SHELL_PATH_TTL_SECONDS, frozen=True, description="How long one read lasts"
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _cached_path: str | None = PrivateAttr(default=None)
    _cached_at: float | None = PrivateAttr(default=None)

    def read(self) -> str:
        """The login shell's PATH, or the desktop's own when the shell does not answer in time."""
        with self._lock:
            now = time.monotonic()
            if (
                self._cached_path is not None
                and self._cached_at is not None
                and now - self._cached_at < self.ttl_seconds
            ):
                return self._cached_path
            self._cached_path = self._read_from_shell() or self.environment.get("PATH", "")
            self._cached_at = now
            return self._cached_path

    def _read_from_shell(self) -> str | None:
        shell = self.environment.get("SHELL") or _DEFAULT_SHELL
        try:
            finished = self.concurrency_group.run_process_to_completion(
                [shell, "-lc", 'printf %s "$PATH"'],
                timeout=_LOGIN_SHELL_TIMEOUT_SECONDS,
                env=self.environment,
                name="login shell PATH",
            )
        except ProcessError as e:
            logger.warning("Could not read the login shell's PATH from {}; using the desktop's own: {}", shell, e)
            return None
        # A profile may print on stdout before the PATH; the PATH is the last line.
        last_line = finished.stdout.strip().splitlines()[-1] if finished.stdout.strip() else ""
        return last_line or None
