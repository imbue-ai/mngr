"""Ship one of the programs in ``imbue.minds.workspace_programs`` into a workspace, and read its answer.

A workspace program runs under the workspace's own ``python3`` with none of the
app's code, so it ships as a self-contained zip application: the program's
module becomes the archive's ``__main__.py``, beside every module of the
package it imports (and those modules' imports), at their package paths, so
the shared modules import exactly as they do here. Every file in the archive
is a checked source file of the package, read through ``importlib.resources``
(the package ships in the wheel), plus the empty ``__init__.py`` files that
make its path a package. The archive is deterministic -- the same sources give
the same bytes -- so a workspace that keeps a copy can tell whether it is
current by comparing bytes.

``python3 <archive> <args>`` runs it from any file name, so a copy written into
the workspace runs standalone (from cron, say). Through ``mngr exec`` the
archive travels base64-encoded inside the command (the base64 alphabet holds
no shell-significant characters), is decoded to a temporary file, run, and
removed; parameters travel as plain argv. Each program answers on one
marker-prefixed JSON line, which :func:`extract_marker_json` finds in
arbitrarily noisy output.
"""

import ast
import base64
import io
import json
import shlex
import zipfile
from collections.abc import Sequence
from functools import cache
from importlib import resources
from types import ModuleType
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds import workspace_programs
from imbue.minds.errors import MindError

WORKSPACE_PROGRAMS_PACKAGE: Final[str] = workspace_programs.__name__

_ARCHIVE_MAIN_PATH: Final[str] = "__main__.py"
# Fixed entry metadata, so an archive's bytes depend on its sources alone.
_ARCHIVE_ENTRY_DATE_TIME: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)
_ARCHIVE_ENTRY_MODE: Final[int] = 0o644


class NotAWorkspaceProgramError(MindError):
    """Raised when a module outside the workspace programs package is shipped as a workspace program."""

    ...


def _package_relative_name(module_name: str) -> str:
    prefix = f"{WORKSPACE_PROGRAMS_PACKAGE}."
    if not module_name.startswith(prefix):
        raise NotAWorkspaceProgramError(f"{module_name} is not a module of {WORKSPACE_PROGRAMS_PACKAGE}")
    return module_name.removeprefix(prefix)


def _read_package_module_source(module_name: str) -> str:
    source_file = resources.files(WORKSPACE_PROGRAMS_PACKAGE).joinpath(f"{_package_relative_name(module_name)}.py")
    return source_file.read_text(encoding="utf-8")


@pure
def imported_package_module_names(source: str) -> tuple[str, ...]:
    """The modules of the workspace programs package that ``source`` imports, in the order it imports them."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == WORKSPACE_PROGRAMS_PACKAGE:
            names.extend(f"{WORKSPACE_PROGRAMS_PACKAGE}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(f"{WORKSPACE_PROGRAMS_PACKAGE}."):
            names.append(node.module or "")
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names if alias.name.startswith(f"{WORKSPACE_PROGRAMS_PACKAGE}."))
        else:
            pass
    return tuple(dict.fromkeys(names))


def _dependency_sources_by_archive_path(main_source: str) -> dict[str, str]:
    """Every package module ``main_source`` imports, directly or through another, keyed by its archive path."""
    source_by_module_name: dict[str, str] = {}
    pending = list(imported_package_module_names(main_source))
    while pending:
        module_name = pending.pop()
        if module_name in source_by_module_name:
            continue
        source = _read_package_module_source(module_name)
        source_by_module_name[module_name] = source
        pending.extend(imported_package_module_names(source))
    return {f"{name.replace('.', '/')}.py": source for name, source in source_by_module_name.items()}


def _package_init_archive_paths() -> tuple[str, ...]:
    """The ``__init__.py`` of the package and of each package above it, outermost first."""
    parts = WORKSPACE_PROGRAMS_PACKAGE.split(".")
    return tuple("/".join(parts[: depth + 1]) + "/__init__.py" for depth in range(len(parts)))


def build_workspace_program_archive(program: ModuleType) -> bytes:
    """``program`` as a self-contained zip application, runnable as ``python3 <archive>``."""
    return _build_archive_of_module(program.__name__)


@cache
def _build_archive_of_module(module_name: str) -> bytes:
    """The archive of the program module ``module_name``, built once per process: its sources cannot change."""
    main_source = _read_package_module_source(module_name)
    text_by_archive_path = {
        **{path: "" for path in _package_init_archive_paths()},
        **_dependency_sources_by_archive_path(main_source),
        _ARCHIVE_MAIN_PATH: main_source,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for archive_path in sorted(text_by_archive_path):
            entry = zipfile.ZipInfo(archive_path, date_time=_ARCHIVE_ENTRY_DATE_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = _ARCHIVE_ENTRY_MODE << 16
            archive.writestr(entry, text_by_archive_path[archive_path])
    return buffer.getvalue()


def build_workspace_program_command(
    program: ModuleType,
    args: Sequence[str],
    # The shell words that start the workspace's python, with any environment assignments in front.
    python_command: str = "python3",
) -> str:
    """The shell command that runs ``program`` with argv ``args`` from the workspace's current directory.

    It exits with the program's own exit status, and removes the archive's temporary file either way.
    """
    encoded = base64.b64encode(build_workspace_program_archive(program)).decode("ascii")
    return (
        'minds_program=$(mktemp "${TMPDIR:-/tmp}/minds-program.XXXXXX") && '
        f"printf %s '{encoded}' | base64 -d > \"$minds_program\" && "
        f'{python_command} "$minds_program" {shlex.join(args)}; '
        'minds_program_exit=$?; rm -f "$minds_program"; exit $minds_program_exit'
    )


@pure
def extract_marker_json(stdout: str, marker: str) -> dict[str, object] | None:
    """Extract the last marker-prefixed JSON payload from arbitrary stdout, or None."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith(marker):
            continue
        try:
            payload = json.loads(stripped[len(marker) :])
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
    return None
