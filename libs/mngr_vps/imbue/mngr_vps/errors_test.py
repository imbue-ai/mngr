"""Tests for VPS error hierarchy."""

from imbue.mngr.errors import MngrError
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsError
from imbue.mngr_vps.errors import VpsProvisioningError


def test_error_hierarchy_base() -> None:
    assert issubclass(VpsError, MngrError)


def test_error_hierarchy_provisioning() -> None:
    assert issubclass(VpsProvisioningError, VpsError)


def test_error_hierarchy_api() -> None:
    assert issubclass(VpsApiError, VpsError)


def test_vps_api_error_stores_status_code() -> None:
    err = VpsApiError(404, "Not found")
    assert err.status_code == 404
    assert "404" in str(err)
    assert "Not found" in str(err)


def test_vps_api_error_zero_status_code() -> None:
    err = VpsApiError(0, "Request failed: connection refused")
    assert err.status_code == 0
    assert "connection refused" in str(err)
