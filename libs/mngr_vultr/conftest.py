"""Project-level conftest for mngr_vultr.

When running tests from libs/mngr_vultr/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks

register_conftest_hooks(globals())
