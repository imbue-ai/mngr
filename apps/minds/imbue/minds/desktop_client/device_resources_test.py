from imbue.minds.desktop_client.device_resources import AlertSeverity
from imbue.minds.desktop_client.device_resources import AmountReading
from imbue.minds.desktop_client.device_resources import CpuReading
from imbue.minds.desktop_client.device_resources import DeviceReadings
from imbue.minds.desktop_client.device_resources import DeviceResourceSettings
from imbue.minds.desktop_client.device_resources import DeviceResourceView
from imbue.minds.desktop_client.device_resources import DiskReading
from imbue.minds.desktop_client.device_resources import GIB
from imbue.minds.desktop_client.device_resources import GaugeResource
from imbue.minds.desktop_client.device_resources import alerts_for
from imbue.minds.desktop_client.device_resources import estimate_seconds_to_full
from imbue.minds.desktop_client.device_resources import format_duration
from imbue.minds.desktop_client.device_resources import format_gigabytes
from imbue.minds.desktop_client.device_resources import is_push_due
from imbue.minds.desktop_client.device_resources import next_warnings

_TOTAL = 500 * GIB


def _disk(free_gb: float, seconds_to_full: float | None = None) -> DiskReading:
    free = int(free_gb * GIB)
    return DiskReading(used_bytes=_TOTAL - free, free_bytes=free, total_bytes=_TOTAL, seconds_to_full=seconds_to_full)


def _readings(
    disk: DiskReading | None = None, memory_percent: float | None = None, cpu_recent: float | None = None
) -> DeviceReadings:
    memory = (
        None
        if memory_percent is None
        else AmountReading(
            used_bytes=int(memory_percent * GIB), free_bytes=int((100 - memory_percent) * GIB), total_bytes=100 * GIB
        )
    )
    cpu = None if cpu_recent is None else CpuReading(percent=cpu_recent, recent_percent=cpu_recent, cores=8)
    return DeviceReadings(cpu=cpu, memory=memory, disk=disk)


def _view(readings: DeviceReadings, settings: DeviceResourceSettings | None = None) -> DeviceResourceView:
    effective = settings or DeviceResourceSettings()
    return DeviceResourceView(
        settings=effective,
        readings=readings,
        alerts=alerts_for(next_warnings(frozenset(), readings, effective), readings, effective),
    )


def test_formats_say_sizes_and_spans_roughly() -> None:
    assert format_gigabytes(int(2.25 * GIB)) == "2.2 GB"
    assert format_gigabytes(289 * GIB) == "289 GB"
    assert format_duration(30) == "under a minute"
    assert format_duration(60) == "1 minute"
    assert format_duration(12 * 60) == "12 minutes"
    assert format_duration(3 * 3600) == "3 hours"


def test_disk_warns_at_its_level_and_clears_only_with_a_margin() -> None:
    settings = DeviceResourceSettings(disk_warning_free_gb=10)
    raised = next_warnings(frozenset(), _readings(disk=_disk(9)), settings)
    assert raised == {GaugeResource.DISK}
    # Just over the level: a raised warning holds, a fresh reading does not raise one.
    assert next_warnings(raised, _readings(disk=_disk(11)), settings) == {GaugeResource.DISK}
    assert next_warnings(frozenset(), _readings(disk=_disk(11)), settings) == frozenset()
    assert next_warnings(raised, _readings(disk=_disk(12.5)), settings) == frozenset()


def test_disk_filling_fast_warns_with_room_to_spare() -> None:
    settings = DeviceResourceSettings(disk_warning_free_gb=10)
    readings = _readings(disk=_disk(200, seconds_to_full=10 * 60))
    alerts = alerts_for(next_warnings(frozenset(), readings, settings), readings, settings)
    assert [(alert.headline, alert.detail) for alert in alerts] == [
        ("Disk is filling up fast", "200 GB left, about 10 minutes at this rate")
    ]


def test_an_unread_resource_keeps_its_warning() -> None:
    settings = DeviceResourceSettings(disk_warning_free_gb=10)
    assert next_warnings(frozenset({GaugeResource.DISK}), _readings(), settings) == {GaugeResource.DISK}
    assert next_warnings(frozenset(), _readings(), settings) == frozenset()


def test_memory_and_cpu_warn_only_once_given_a_level() -> None:
    busy = _readings(memory_percent=97, cpu_recent=98)
    assert next_warnings(frozenset(), busy, DeviceResourceSettings()) == frozenset()
    settings = DeviceResourceSettings(memory_warning_percent=90, cpu_warning_percent=90)
    assert next_warnings(frozenset(), busy, settings) == {GaugeResource.MEMORY, GaugeResource.CPU}
    # Within five points under the level, a raised share warning holds.
    assert next_warnings(frozenset({GaugeResource.CPU}), _readings(cpu_recent=86), settings) == {GaugeResource.CPU}
    assert next_warnings(frozenset({GaugeResource.CPU}), _readings(cpu_recent=85), settings) == frozenset()


def test_critical_alerts_come_first() -> None:
    settings = DeviceResourceSettings(disk_warning_free_gb=10, memory_warning_percent=90)
    readings = _readings(disk=_disk(1), memory_percent=95)
    alerts = alerts_for(frozenset({GaugeResource.MEMORY, GaugeResource.DISK}), readings, settings)
    assert [(alert.resource, alert.severity, alert.headline) for alert in alerts] == [
        (GaugeResource.DISK, AlertSeverity.CRITICAL, "Disk is almost full"),
        (GaugeResource.MEMORY, AlertSeverity.WARNING, "Memory is nearly full"),
    ]


def test_time_to_full_needs_a_minute_of_filling() -> None:
    assert estimate_seconds_to_full([(0.0, 100 * GIB)], 100 * GIB) is None
    assert estimate_seconds_to_full([(0.0, 100 * GIB), (30.0, 99 * GIB)], 99 * GIB) is None
    assert estimate_seconds_to_full([(0.0, 100 * GIB), (60.0, 100 * GIB)], 100 * GIB) is None
    assert estimate_seconds_to_full([(0.0, 12 * GIB), (60.0, 10 * GIB)], 10 * GIB) == 300.0


def test_a_push_is_due_for_a_change_of_warnings_or_levels_only() -> None:
    calm = _view(_readings(disk=_disk(200), memory_percent=40, cpu_recent=10))
    assert is_push_due(None, calm)
    assert not is_push_due(calm, _view(_readings(disk=_disk(150), memory_percent=80, cpu_recent=70)))
    assert is_push_due(calm, _view(_readings(disk=_disk(5), memory_percent=40, cpu_recent=10)))
    assert is_push_due(calm, _view(calm.readings, DeviceResourceSettings(memory_warning_percent=90)))
