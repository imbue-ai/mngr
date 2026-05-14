"""Provisioning helper for agents that satisfy ``HasCommonTranscriptMixin``.

Writes the per-agent converter scripts returned by
``get_common_transcript_scripts`` to ``$MNGR_AGENT_STATE_DIR/commands/`` at
mode ``0755`` so the agent's ``assemble_command`` can launch them.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Mapping

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.logging import log_span
from imbue.mngr.interfaces.host import OnlineHostInterface


def provision_common_transcript_scripts(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    scripts: Mapping[str, str],
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Write transcript converter scripts to ``$MNGR_AGENT_STATE_DIR/commands/``.

    Each ``(name, content)`` pair is uploaded in parallel via the concurrency
    group at mode ``0755``. Returns once every upload thread has joined.
    """
    commands_dir = agent_state_dir / "commands"
    host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(commands_dir))}", timeout_seconds=5.0)

    threads: list[ObservableThread] = []
    for script_name, script_content in scripts.items():
        script_path = commands_dir / script_name
        with log_span("Writing {} to agent state dir", script_name):
            try:
                thread = concurrency_group.start_new_thread(
                    host.write_file, (script_path, script_content.encode(), "0755")
                )
            except InvalidConcurrencyGroupStateError:
                logger.debug("Concurrency group shutting down; aborting transcript script provisioning")
                return
            threads.append(thread)

    for thread in threads:
        thread.join(60.0)
