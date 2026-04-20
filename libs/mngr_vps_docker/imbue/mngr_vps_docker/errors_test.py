"""Tests for VPS Docker error hierarchy."""

from imbue.mngr.errors import MngrError
from imbue.mngr_vps_docker.errors import ContainerSetupError
from imbue.mngr_vps_docker.errors import DockerNotReadyError
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsConnectionError
from imbue.mngr_vps_docker.errors import VpsDockerError
from imbue.mngr_vps_docker.errors import VpsProvisioningError


def test_error_hierarchy_base() -> None:
    assert issubclass(VpsDockerError, MngrError)


def test_error_hierarchy_provisioning() -> None:
    assert issubclass(VpsProvisioningError, VpsDockerError)


def test_error_hierarchy_connection() -> None:
    assert issubclass(VpsConnectionError, VpsDockerError)
    assert issubclass(VpsConnectionError, ConnectionError)


def test_error_hierarchy_docker_not_ready() -> None:
    assert issubclass(DockerNotReadyError, VpsDockerError)


def test_error_hierarchy_container_setup() -> None:
    assert issubclass(ContainerSetupError, VpsDockerError)


def test_error_hierarchy_api() -> None:
    assert issubclass(VpsApiError, VpsDockerError)


def test_vps_api_error_stores_status_code() -> None:
    err = VpsApiError(404, "Not found")
    assert err.status_code == 404
    assert "404" in str(err)
    assert "Not found" in str(err)


def test_vps_api_error_zero_status_code() -> None:
    err = VpsApiError(0, "Request failed: connection refused")
    assert err.status_code == 0
    assert "connection refused" in str(err)
