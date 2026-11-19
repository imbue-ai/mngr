"""Tests for the /ui/api resource gauge routes (the card's read, and saving the settings)."""

from pathlib import Path

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.device_resource_monitor import DeviceResourceMonitor
from imbue.minds.desktop_client.device_resource_monitor import DeviceResourceSettingsStore
from imbue.minds.desktop_client.device_resources import DeviceReadings
from imbue.minds.desktop_client.device_resources import DiskReading
from imbue.minds.desktop_client.device_resources import GIB
from imbue.minds.desktop_client.mock_device_resource_monitor_test import FakeDeviceResourceReader


def _monitor(tmp_path: Path) -> DeviceResourceMonitor:
    disk = DiskReading(used_bytes=480 * GIB, free_bytes=20 * GIB, total_bytes=500 * GIB)
    monitor = DeviceResourceMonitor(
        reader=FakeDeviceResourceReader(device=DeviceReadings(cpu=None, memory=None, disk=disk)),
        settings_store=DeviceResourceSettingsStore(data_dir=tmp_path / "gauge"),
    )
    (tmp_path / "gauge").mkdir()
    monitor.take_reading()
    return monitor


def test_the_card_reads_the_gauge_and_saving_settings_rejudges_it(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path)
    client, _app, _auth = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, device_resource_monitor=monitor
    )

    read = client.get("/ui/api/device-resources")
    assert read.status_code == 200
    assert read.get_json()["readings"]["disk"]["free_bytes"] == 20 * GIB
    assert read.get_json()["alerts"] == []

    saved = client.post(
        "/ui/api/device-resources/settings",
        json={"disk_warning_free_gb": 25, "memory_warning_percent": None},
    )
    assert saved.status_code == 200
    assert [alert["headline"] for alert in saved.get_json()["alerts"]] == ["Disk space is low"]
    assert monitor.view().settings.disk_warning_free_gb == 25


def test_settings_out_of_range_are_refused_and_nothing_is_saved(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path)
    client, _app, _auth = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, device_resource_monitor=monitor
    )

    response = client.post("/ui/api/device-resources/settings", json={"cpu_warning_percent": 10})

    assert response.status_code == 400
    assert monitor.view().settings.cpu_warning_percent is None


def test_routes_require_authentication_and_a_monitor(tmp_path: Path) -> None:
    unauthenticated, _app, _auth = build_desktop_client_for_test(
        tmp_path / "a", is_authenticated=False, device_resource_monitor=_monitor(tmp_path)
    )
    assert unauthenticated.get("/ui/api/device-resources").status_code == 401
    without_monitor, _app, _auth = build_desktop_client_for_test(tmp_path / "b", is_authenticated=True)
    assert without_monitor.get("/ui/api/device-resources").status_code == 503
