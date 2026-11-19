import json
from pathlib import Path

from imbue.minds.desktop_client.device_resource_monitor import DeviceResourceMonitor
from imbue.minds.desktop_client.device_resource_monitor import DeviceResourceSettingsStore
from imbue.minds.desktop_client.device_resources import CpuReading
from imbue.minds.desktop_client.device_resources import DEFAULT_DEVICE_RESOURCE_SETTINGS
from imbue.minds.desktop_client.device_resources import DeviceReadings
from imbue.minds.desktop_client.device_resources import DeviceResourceSettings
from imbue.minds.desktop_client.device_resources import DeviceResourceView
from imbue.minds.desktop_client.device_resources import DiskReading
from imbue.minds.desktop_client.device_resources import GIB
from imbue.minds.desktop_client.device_resources import GaugeResource
from imbue.minds.desktop_client.mock_device_resource_monitor_test import FakeDeviceResourceReader

_TOTAL = 500 * GIB


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _disk(free_gb: float) -> DiskReading:
    free = int(free_gb * GIB)
    return DiskReading(used_bytes=_TOTAL - free, free_bytes=free, total_bytes=_TOTAL)


def _monitor(tmp_path: Path, reader: FakeDeviceResourceReader, clock: _Clock) -> tuple[DeviceResourceMonitor, list]:
    monitor = DeviceResourceMonitor(
        reader=reader, settings_store=DeviceResourceSettingsStore(data_dir=tmp_path), clock=clock
    )
    pushed: list[DeviceResourceView] = []
    monitor.add_on_push_callback(pushed.append)
    return monitor, pushed


def test_pushes_the_first_reading_then_only_a_change_of_warnings(tmp_path: Path) -> None:
    clock = _Clock()
    reader = FakeDeviceResourceReader(device=DeviceReadings(cpu=None, memory=None, disk=_disk(200)))
    monitor, pushed = _monitor(tmp_path, reader, clock)

    monitor.take_reading()
    clock.now += 5
    monitor.take_reading()
    assert len(pushed) == 1

    reader.device = DeviceReadings(cpu=None, memory=None, disk=_disk(5))
    clock.now += 5
    monitor.take_reading()
    assert len(pushed) == 2
    assert [alert.headline for alert in pushed[-1].alerts] == ["Disk space is low"]


def test_pushes_once_a_minute_even_when_nothing_changes(tmp_path: Path) -> None:
    clock = _Clock()
    reader = FakeDeviceResourceReader(device=DeviceReadings(cpu=None, memory=None, disk=_disk(200)))
    monitor, pushed = _monitor(tmp_path, reader, clock)
    monitor.take_reading()
    clock.now += 61
    monitor.take_reading()
    assert len(pushed) == 2


def test_averages_the_cpu_over_the_warning_window(tmp_path: Path) -> None:
    clock = _Clock()
    reader = FakeDeviceResourceReader()
    monitor, _pushed = _monitor(tmp_path, reader, clock)
    for percent in (20.0, 40.0, 60.0):
        reader.device = DeviceReadings(
            cpu=CpuReading(percent=percent, recent_percent=percent, cores=8), memory=None, disk=None
        )
        monitor.take_reading()
        clock.now += 5
    cpu = monitor.view().readings.cpu
    assert cpu is not None
    assert (cpu.percent, cpu.recent_percent) == (60.0, 40.0)


def test_projects_when_the_disk_fills(tmp_path: Path) -> None:
    clock = _Clock()
    reader = FakeDeviceResourceReader(device=DeviceReadings(cpu=None, memory=None, disk=_disk(12)))
    monitor, _pushed = _monitor(tmp_path, reader, clock)
    monitor.take_reading()
    clock.now += 60
    reader.device = DeviceReadings(cpu=None, memory=None, disk=_disk(10))
    monitor.take_reading()
    disk = monitor.view().readings.disk
    assert disk is not None
    assert disk.seconds_to_full == 300.0


def test_new_settings_are_kept_judged_at_once_and_pushed(tmp_path: Path) -> None:
    clock = _Clock()
    reader = FakeDeviceResourceReader(device=DeviceReadings(cpu=None, memory=None, disk=_disk(30)))
    monitor, pushed = _monitor(tmp_path, reader, clock)
    monitor.take_reading()
    assert monitor.view().alerts == ()

    view = monitor.update_settings(DeviceResourceSettings(disk_warning_free_gb=50))

    assert [alert.resource for alert in view.alerts] == [GaugeResource.DISK]
    assert pushed[-1] == view
    assert DeviceResourceSettingsStore(data_dir=tmp_path).read() == DeviceResourceSettings(disk_warning_free_gb=50)


def test_settings_store_falls_back_to_defaults_on_anything_unusable(tmp_path: Path) -> None:
    store = DeviceResourceSettingsStore(data_dir=tmp_path)
    assert store.read() == DEFAULT_DEVICE_RESOURCE_SETTINGS
    store.path.write_text("{not json")
    assert store.read() == DEFAULT_DEVICE_RESOURCE_SETTINGS
    store.path.write_text(json.dumps({"version": 99, "disk_warning_free_gb": 20}))
    assert store.read() == DEFAULT_DEVICE_RESOURCE_SETTINGS
    store.path.write_text(json.dumps({"version": 1, "disk_warning_free_gb": 100000}))
    assert store.read() == DEFAULT_DEVICE_RESOURCE_SETTINGS
