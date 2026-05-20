"""Provisioning helpers for agents that satisfy the transcript mixins.

Three helpers live here:

- :func:`provision_scripts_to_commands_dir` is a generic primitive that
  uploads any ``{name: content}`` mapping to ``$MNGR_AGENT_STATE_DIR/commands/``
  in parallel. It is reused by per-agent code to upload non-transcript
  helper scripts (e.g. Claude's ``wait_for_stop_hook.sh``).
- :func:`provision_raw_transcript_scripts` unconditionally provisions the
  scripts returned by
  :meth:`HasTranscriptMixin.get_raw_transcript_scripts`. Raw capture is
  not user-gated because it is the source of truth for the agent session.
- :func:`maybe_provision_common_transcript_scripts` is the gated
  common-transcript entry point: it reads
  :attr:`HasCommonTranscriptMixin.is_common_transcript_enabled` on the
  agent and either provisions the scripts returned by
  :meth:`HasCommonTranscriptMixin.get_common_transcript_scripts` or does
  nothing. Subclasses' ``provision`` methods should call this rather than
  rolling their own gate.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.logging import log_span
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasTranscriptMixin
from imbue.mngr.interfaces.host import OnlineHostInterface


def provision_scripts_to_commands_dir(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    scripts: Mapping[str, str],
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Write a ``{name: content}`` mapping of shell scripts to ``$MNGR_AGENT_STATE_DIR/commands/``.

    Each entry is uploaded in parallel via the concurrency group at mode
    ``0755``. Returns once every upload thread has joined. Generic --
    callers use this for transcript converters as well as for unrelated
    background helpers (Claude's ``wait_for_stop_hook.sh``, etc.).
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
                logger.debug("Concurrency group shutting down; aborting script provisioning")
                return
            threads.append(thread)

    for thread in threads:
        thread.join(60.0)


def provision_raw_transcript_scripts(
    agent: HasTranscriptMixin,
    host: OnlineHostInterface,
    agent_state_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Provision the agent's raw-transcript capture scripts.

    Unconditional: raw capture is the source of truth for the agent's
    session and is not user-gated. Uploads the scripts returned by
    :meth:`HasTranscriptMixin.get_raw_transcript_scripts` to
    ``$MNGR_AGENT_STATE_DIR/commands/`` via
    :func:`provision_scripts_to_commands_dir`.
    """
    provision_scripts_to_commands_dir(host, agent_state_dir, agent.get_raw_transcript_scripts(), concurrency_group)


def maybe_provision_common_transcript_scripts(
    agent: HasCommonTranscriptMixin,
    host: OnlineHostInterface,
    agent_state_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Provision the agent's transcript scripts if it has opted in.

    Reads :attr:`HasCommonTranscriptMixin.is_common_transcript_enabled` on
    ``agent``; if False, returns without writing anything. Otherwise
    uploads the scripts returned by
    :meth:`HasCommonTranscriptMixin.get_common_transcript_scripts` to
    ``$MNGR_AGENT_STATE_DIR/commands/`` via
    :func:`provision_scripts_to_commands_dir`.
    """
    if not agent.is_common_transcript_enabled:
        return
    provision_scripts_to_commands_dir(host, agent_state_dir, agent.get_common_transcript_scripts(), concurrency_group)
