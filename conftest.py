"""Root conftest for the monorepo.

Common pytest hooks (test locking, timing limits, output file redirection) are
provided by the shared module imbue.imbue_common.conftest_hooks. Each project's
conftest.py calls register_conftest_hooks(globals()) to inject them. The shared
module ensures hooks are only registered once even when multiple conftest.py files
are discovered (e.g., when running from the monorepo root).
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mng.register_guards_docker import register_docker_cli_guard
from imbue.mng.register_guards_docker import register_docker_sdk_guard
from imbue.mng.utils.logging import suppress_warnings
from imbue.mng_modal.register_guards import register_modal_guard
from imbue.resource_guards.resource_guards import register_resource_guard

# Suppress some pointless warnings from other library's loggers
suppress_warnings()

# Register mng-specific pytest markers and guarded resources.
register_marker("tmux: marks tests that create real tmux sessions or mng agents")
register_marker("rsync: marks tests that invoke rsync for file transfer")
register_marker("unison: marks tests that start a real unison file-sync process")
register_marker("modal: marks tests that connect to the Modal cloud service")
register_marker("docker: marks tests that invoke the docker CLI via subprocess")
register_marker("docker_sdk: marks tests that use the Docker Python SDK in-process")
register_resource_guard("tmux")
register_resource_guard("rsync")
register_resource_guard("unison")
register_modal_guard()
register_docker_cli_guard()
register_docker_sdk_guard()

register_conftest_hooks(globals())
