"""Resource guard registration for the docker CLI and SDK.

Discovered via the resource_guards entry point group declared in
mngr_docker's pyproject.toml, so every conftest that calls
``register_conftest_hooks`` picks these up when the plugin is installed.
"""

from docker.api.client import APIClient

from imbue.mngr_docker.leaked_container_sweep import sweep_leaked_test_containers
from imbue.resource_guards.resource_guards import MethodKind
from imbue.resource_guards.resource_guards import create_sdk_method_guard
from imbue.resource_guards.resource_guards import register_resource_guard
from imbue.resource_guards.resource_guards import register_session_cleanup_callback


def register_docker_guards() -> None:
    """Register the docker CLI guard, the docker SDK guard, and the leaked-container sweep.

    The CLI guard is a PATH wrapper that intercepts docker subprocess calls,
    including from child processes launched by mngr create. The SDK guard
    monkeypatches APIClient.send to intercept in-process Docker SDK HTTP
    calls. The sweep runs once per pytest session to remove test containers
    that a fixture failed to clean up. All three are safe to register more
    than once.
    """
    register_resource_guard("docker")
    create_sdk_method_guard("docker_sdk", [(APIClient, "send", MethodKind.SYNC)])
    register_session_cleanup_callback(sweep_leaked_test_containers)
