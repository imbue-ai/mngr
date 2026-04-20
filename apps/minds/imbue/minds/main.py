# ruff: noqa: E402
"""CLI entrypoint that bootstraps MNGR_* env vars before loading the CLI.

Mngr reads ``MNGR_HOST_DIR``/``MNGR_PREFIX`` at module import time (plugin
manager construction, config discovery). The bootstrap must therefore run
before any ``imbue.mngr.*`` import, which is why apply_bootstrap() runs as
an import-time side effect here -- ordered strictly *before* the cli_entry
import that transitively loads mngr. This is why E402 (import-not-at-top)
is disabled for this file.
"""

from imbue.minds.bootstrap import apply_bootstrap

apply_bootstrap()

from imbue.minds.cli_entry import cli


def main() -> None:
    """CLI entrypoint. The real bootstrap already ran at module import time."""
    cli()
