"""ANSI color palette and the color-enable policy shared across mngr output.

This is a leaf module (stdlib-only imports) so it can sit below every layer of
the import-linter ``mngr layers contract`` -- in particular ``imbue.mngr.errors``
imports it for ``MngrError.show`` while ``imbue.mngr.utils.logging`` imports it
for the loguru ``WARNING:``/``ERROR:`` prefixes, keeping both renderers on one
source of truth without either depending on the other.
"""

import os
import sys
from typing import Any
from typing import IO

# ANSI color codes that work well on both light and dark backgrounds.
# Using 256-color palette codes with bold for better visibility.
# Falls back gracefully in terminals that don't support 256 colors.
# WARNING_COLOR: Bold gold/orange (256-color code 178)
# ERROR_COLOR: Bold red (256-color code 196)
# BUILD_COLOR: Medium gray (256-color code 245) - visible on both black and white backgrounds
# DEBUG_COLOR: Solid blue (256-color code 33)
# TRACE_COLOR: Purple (256-color code 99)
WARNING_COLOR = "\x1b[1;38;5;178m"
ERROR_COLOR = "\x1b[1;38;5;196m"
BUILD_COLOR = "\x1b[38;5;245m"
DEBUG_COLOR = "\x1b[38;5;33m"
TRACE_COLOR = "\x1b[38;5;99m"
RESET_COLOR = "\x1b[0m"


def should_use_color(stream: IO[Any] | None = None) -> bool:
    """Check whether ANSI color codes should be used on the given stream.

    Respects the NO_COLOR convention (https://no-color.org/) and falls back
    to checking whether the stream is a TTY. When stream is None, defaults
    to sys.stderr.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    target = stream if stream is not None else sys.stderr
    try:
        return target.isatty()
    except (AttributeError, ValueError):
        return False
