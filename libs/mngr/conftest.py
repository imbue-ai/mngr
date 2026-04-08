"""Project-level conftest for mngr.

When running tests from libs/mngr/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.register_guards_docker import register_docker_cli_guard
from imbue.mngr.register_guards_docker import register_docker_sdk_guard
from imbue.mngr.utils.logging import suppress_warnings
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

# Register mngr-specific pytest markers and guarded resources.
register_marker("tmux: marks tests that create real tmux sessions or mngr agents")
register_marker("rsync: marks tests that invoke rsync for file transfer")
register_marker("unison: marks tests that start a real unison file-sync process")
register_marker("modal: marks tests that connect to the Modal cloud service")
register_marker("docker: marks tests that invoke the docker CLI via subprocess")
register_marker("docker_sdk: marks tests that use the Docker Python SDK in-process")
register_resource_guard("tmux")
register_resource_guard("modal")
register_resource_guard("rsync")
register_resource_guard("unison")
register_docker_cli_guard()
register_docker_sdk_guard()

register_conftest_hooks(globals())
