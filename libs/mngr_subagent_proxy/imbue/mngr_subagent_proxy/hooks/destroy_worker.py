"""Module entry-point for a detached single-agent destroy worker.

Invoked by ``hooks/rewrite.py`` and ``hooks/reap.py`` as::

    python -m imbue.mngr_subagent_proxy.hooks.destroy_worker <target_name>

The module intentionally does as little as possible: resolve the agent
by name and call ``execute_cleanup`` with ``CleanupAction.DESTROY``.
"""

from __future__ import annotations

import sys

from loguru import logger

from imbue.mngr_subagent_proxy.hooks.mngr_api import destroy_agent_sync


def main() -> None:
    if len(sys.argv) != 2:
        logger.error("destroy_worker: usage: python -m ... destroy_worker <target_name>")
        sys.exit(2)
    destroy_agent_sync(sys.argv[1])


if __name__ == "__main__":
    main()
