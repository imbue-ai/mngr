from docker.api.client import APIClient

from imbue.resource_guards.resource_guards import MethodKind
from imbue.resource_guards.resource_guards import create_sdk_method_guard
from imbue.resource_guards.resource_guards import register_resource_guard


def register_docker_cli_guard() -> None:
    """Register the Docker CLI binary guard. Safe to call multiple times.

    Uses a PATH wrapper to intercept docker CLI subprocess calls, including
    from child processes launched by mngr create.
    """
    register_resource_guard("docker")


def register_docker_sdk_guard() -> None:
    """Register the Docker SDK guard. Safe to call multiple times.

    Monkeypatches APIClient.send to intercept in-process Docker SDK HTTP calls.
    """
    create_sdk_method_guard("docker_sdk", [(APIClient, "send", MethodKind.SYNC)])
