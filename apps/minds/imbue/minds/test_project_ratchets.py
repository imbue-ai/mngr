from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet
from imbue.imbue_common.ratchet_testing.core import format_ratchet_failure_message
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS

_DIR = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")


# --- Logging ---


def test_prevent_logger_warning() -> None:
    # ``scripts/*.py`` are operator / e2e driver scripts that print
    # step-by-step progress at warning level and are not part of the
    # shipped ``imbue`` wheel; test files (excluded via TEST_FILE_PATTERNS)
    # are likewise out of scope.
    excluded = TEST_FILE_PATTERNS + ("scripts/*.py",)
    # Catch both the plain ``logger.warning(`` call and the
    # ``logger.opt(...).warning(`` form used to attach a traceback.
    pattern = RegexPattern(r"\w*logger(\.opt\([^)]*\))?\.warning\(", multiline=False)
    chunks = check_regex_ratchet(_DIR, FileExtension(".py"), pattern, excluded)
    assert len(chunks) <= snapshot(0), format_ratchet_failure_message(
        rule_name="logger.warning() usages",
        rule_description=(
            "Do not use logger.warning(). A log line is either an error that should never happen -- "
            "in which case log it with logger.error() (or logger.opt(exception=exc).error() to keep "
            "the traceback), which is automatically reported to Sentry -- or it is expected, "
            "business-as-usual behavior, in which case use logger.info(). The warning level is the "
            "lazy middle ground that hides whether a condition actually needs attention."
        ),
        chunks=chunks,
    )
