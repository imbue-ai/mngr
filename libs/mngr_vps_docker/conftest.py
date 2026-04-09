"""Project-level conftest for mngr_vps_docker.

When running tests from libs/mngr_vps_docker/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks

register_conftest_hooks(globals())
