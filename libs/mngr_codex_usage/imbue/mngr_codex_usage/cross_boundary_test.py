"""Guard the writer-script <-> reader and supervisor seams.

The Codex usage writer (`codex_usage.sh`, this package) emits under a source the
reader here claims, and mngr_codex's background-tasks supervisor launches it by
filename. Both agreements are hand-synced string literals (across the shell <->
Python boundary, and across packages), so a rename silently disables usage
tracking. Pin both invariants.
"""

from __future__ import annotations

import importlib.resources

from imbue.mngr_codex.codex_config import BACKGROUND_TASKS_SCRIPT_NAME
from imbue.mngr_codex.plugin import _load_codex_resource_script
from imbue.mngr_codex_usage import resources as _resources
from imbue.mngr_codex_usage.plugin import _CODEX_USAGE_SOURCE_NAME
from imbue.mngr_codex_usage.plugin import _USAGE_EMIT_SCRIPT
from imbue.mngr_codex_usage.plugin import _USAGE_WRITER_SCRIPT


def test_writer_script_emits_the_source_the_reader_claims() -> None:
    # The emitted source literal lives in the python emitter the writer invokes.
    emit_py = importlib.resources.files(_resources).joinpath(_USAGE_EMIT_SCRIPT).read_text()
    assert f'"{_CODEX_USAGE_SOURCE_NAME}/usage"' in emit_py


def test_writer_script_invokes_the_emitter_it_ships_with() -> None:
    writer_sh = importlib.resources.files(_resources).joinpath(_USAGE_WRITER_SCRIPT).read_text()
    assert _USAGE_EMIT_SCRIPT in writer_sh


def test_supervisor_launches_the_writer_by_the_name_this_package_installs() -> None:
    supervisor_sh = _load_codex_resource_script(BACKGROUND_TASKS_SCRIPT_NAME)
    # mngr_codex's supervisor must reference the exact script filename this
    # package installs into commands/, or the writer never launches.
    assert _USAGE_WRITER_SCRIPT in supervisor_sh
