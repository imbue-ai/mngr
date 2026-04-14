"""Root conftest for the monorepo.

Common pytest hooks (test locking, timing limits, output file redirection) are
provided by the shared module imbue.imbue_common.conftest_hooks. Each project's
conftest.py calls register_conftest_hooks(globals()) to inject them. The shared
module ensures hooks are only registered once even when multiple conftest.py files
are discovered (e.g., when running from the monorepo root).
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.register_guards_docker import register_docker_cli_guard
from imbue.mngr.register_guards_docker import register_docker_sdk_guard
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr_modal.register_guards import register_modal_guard
from imbue.resource_guards.resource_guards import register_resource_guard

# Suppress some pointless warnings from other library's loggers
suppress_warnings()

# Register mngr-specific guarded resources.
# The corresponding pytest marks are auto-registered by conftest_hooks.
register_resource_guard("tmux")
register_resource_guard("rsync")
register_resource_guard("unison")
register_modal_guard()
register_docker_cli_guard()
register_docker_sdk_guard()

register_conftest_hooks(globals())
