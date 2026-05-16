"""Module-level constants for mngr_aws.

Kept separate from ``conftest.py`` so test code and library code can import
these without depending on pytest collection semantics (importing from
``conftest.py`` is a pytest anti-pattern; conftest files are auto-discovered
by pytest, not designed for direct import). Mirrors the
``libs/mngr_modal/imbue/mngr_modal/constants.py`` pattern.
"""

from typing import Final

# ``Name`` tag prefix used by release tests when naming their hosts; the
# session-end orphan scan uses this prefix to find instances that escaped
# any per-test cleanup.
AWS_TEST_NAME_PREFIX: Final[str] = "test-aws-"
