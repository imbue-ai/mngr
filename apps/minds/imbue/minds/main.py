# ruff: noqa: E402
"""CLI entrypoint that bootstraps MNGR_* env vars and settings before loading the CLI.

Mngr reads ``MNGR_HOST_DIR``/``MNGR_PREFIX`` and the profile settings.toml at module import time (plugin manager construction, config discovery).
Both the env-var translation and the settings reconciliation must therefore run before any ``imbue.mngr.*`` import, which is why they run as import-time side effects here -- ordered strictly *before* the cli_entry import that transitively loads mngr.
This is why E402 (import-not-at-top) is disabled for this file.
"""

import sys

from imbue.minds.bootstrap import BootstrapError
from imbue.minds.bootstrap import apply_bootstrap

try:
    apply_bootstrap()
except BootstrapError as e:
    # Print the actionable one-liner instead of a traceback: this is operator
    # misconfiguration (e.g. a stale env var), not a program bug.
    print(f"error: {e}", file=sys.stderr)
    raise SystemExit(1) from e

from imbue.minds.mngr_settings.reconcile import ensure_mngr_settings_before_mngr_import

ensure_mngr_settings_before_mngr_import()

from imbue.minds.cli_entry import cli


def main() -> None:
    """CLI entrypoint. The real bootstrap already ran at module import time."""
    cli()
