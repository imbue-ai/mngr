"""Resource warnings for the computer Imbue Studio runs on: the readings they judge and the rules they warn by.

The twin of the workspace shell's resource gauge (the default workspace template's ``shell/resource_gauge.py``),
measured outside every workspace: how much room this computer has left (disk, memory, CPU), and the warnings
raised. The rules and the words are the workspace gauge's, so the two read alike. The titlebar shows a raised
warning; Settings > This computer shows the readings and the levels.

A warning is raised when a resource passes its level and clears only once the resource is comfortably back (a
fifth more disk than the level, or five points under a percent level), so a reading hovering at the line does not
make the pill flicker. The disk also warns when it is on course to fill within fifteen minutes. A resource with no
level never warns; one that could not be read keeps whatever it had.

This module depends on nothing but pydantic, so the channel's wire models (``ui_models.py``) can carry its types.
"""

from collections.abc import Sequence
from enum import auto
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

GIB: Final[int] = 1024**3

MIN_DISK_WARNING_FREE_GB: Final[float] = 0.5
MAX_DISK_WARNING_FREE_GB: Final[float] = 500.0
DEFAULT_DISK_WARNING_FREE_GB: Final[float] = 10.0
# A raised disk warning clears once this much more than the level is free.
DISK_CLEAR_FACTOR: Final[float] = 1.2
# Free space at or under this share of the level is critical.
DISK_CRITICAL_FACTOR: Final[float] = 0.2
DISK_FILLING_FAST_SECONDS: Final[float] = 15 * 60.0
DISK_RATE_WINDOW_SECONDS: Final[float] = 5 * 60.0
DISK_RATE_MIN_SPAN_SECONDS: Final[float] = 60.0
# A projection further out than this says nothing worth showing.
DISK_PROJECTION_HORIZON_SECONDS: Final[float] = 24 * 3600.0

MIN_WARNING_PERCENT: Final[int] = 50
MAX_WARNING_PERCENT: Final[int] = 99
WARNING_CLEAR_MARGIN_PERCENT: Final[float] = 5.0
CPU_WARNING_WINDOW_SECONDS: Final[float] = 120.0


class GaugeResource(LowerCaseStrEnum):
    """One of the resources a warning can be about."""

    CPU = auto()
    MEMORY = auto()
    DISK = auto()


class AlertSeverity(LowerCaseStrEnum):
    WARNING = auto()
    CRITICAL = auto()


class DeviceResourceSettings(FrozenModel):
    """The user's warning levels, one per resource (None: no warning)."""

    disk_warning_free_gb: float | None = Field(
        default=DEFAULT_DISK_WARNING_FREE_GB,
        ge=MIN_DISK_WARNING_FREE_GB,
        le=MAX_DISK_WARNING_FREE_GB,
        description="The disk space left, in GB, at or below which a warning is raised; None for no disk warning",
    )
    memory_warning_percent: int | None = Field(
        default=None,
        ge=MIN_WARNING_PERCENT,
        le=MAX_WARNING_PERCENT,
        description="The share of memory in use that raises a warning; None for no memory warning",
    )
    cpu_warning_percent: int | None = Field(
        default=None,
        ge=MIN_WARNING_PERCENT,
        le=MAX_WARNING_PERCENT,
        description="The share of the CPU in use, averaged over two minutes, that raises a warning; None for none",
    )


DEFAULT_DEVICE_RESOURCE_SETTINGS: Final[DeviceResourceSettings] = DeviceResourceSettings()


class AmountReading(FrozenModel):
    """How much of a resource measured in bytes is in use and left on this computer."""

    used_bytes: int = Field(ge=0, description="Bytes in use")
    free_bytes: int = Field(ge=0, description="Bytes still available")
    total_bytes: int = Field(gt=0, description="Bytes in all")

    @property
    def percent(self) -> float:
        return 100.0 * self.used_bytes / self.total_bytes


class DiskReading(AmountReading):
    """The disk Studio keeps its data on, with when it fills at the rate it has been filling."""

    seconds_to_full: float | None = Field(
        default=None,
        ge=0,
        description="How long until the disk is full at the rate of the last few minutes; None when it is not "
        "filling, the rate is not known yet, or that is more than a day away",
    )


class CpuReading(FrozenModel):
    """This computer's CPU."""

    percent: float = Field(ge=0, description="The share of all cores in use at the latest reading, in percent")
    recent_percent: float = Field(ge=0, description="The mean share over the warning window, in percent")
    cores: int = Field(ge=1, description="How many cores this computer has")


class DeviceReadings(FrozenModel):
    """One reading of each of this computer's resources; None where it could not be read."""

    cpu: CpuReading | None = Field(description="The CPU, None before there are two samples to compare")
    memory: AmountReading | None = Field(description="Memory, None when it could not be read")
    disk: DiskReading | None = Field(description="The disk Studio keeps its data on, None when it could not be read")


NO_DEVICE_READINGS: Final[DeviceReadings] = DeviceReadings(cpu=None, memory=None, disk=None)


class ResourceAlert(FrozenModel):
    """A raised warning, in the words the titlebar and the settings use."""

    resource: GaugeResource = Field(description="The resource the warning is about")
    severity: AlertSeverity = Field(description="Warning, or critical when the resource is all but gone")
    headline: str = Field(description="A few words naming the problem, for the titlebar")
    detail: str = Field(description="The figures behind it, in a short sentence fragment")


class DeviceResourceView(FrozenModel):
    """What the ``device_resources`` channel frame carries and ``GET /ui/api/device-resources`` answers."""

    settings: DeviceResourceSettings = Field(description="The user's warning levels")
    readings: DeviceReadings = Field(description="The latest reading of each of this computer's resources")
    alerts: tuple[ResourceAlert, ...] = Field(description="The raised warnings, the most severe first")


@pure
def format_gigabytes(byte_count: int) -> str:
    """A byte count as GB the way the warnings say it: one decimal under 10, none from there."""
    gigabytes = byte_count / GIB
    return f"{gigabytes:.0f} GB" if gigabytes >= 10 else f"{gigabytes:.1f} GB"


@pure
def format_duration(seconds: float) -> str:
    """A span of time in the roughest unit that says it: 'under a minute', '12 minutes', '3 hours'."""
    minutes = seconds / 60.0
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        whole = round(minutes)
        return "1 minute" if whole == 1 else f"{whole} minutes"
    return f"{round(minutes / 60.0)} hours"


@pure
def estimate_seconds_to_full(samples: Sequence[tuple[float, int]], free_bytes: int) -> float | None:
    """How long until the disk is full, from ``(time, free bytes)`` samples oldest first and the space left now.

    None when the samples span too little time to say, the disk is not filling, or it fills more than a day out.
    """
    if len(samples) < 2:
        return None
    first_time, first_free = samples[0]
    last_time, last_free = samples[-1]
    span = last_time - first_time
    if span < DISK_RATE_MIN_SPAN_SECONDS:
        return None
    rate = (first_free - last_free) / span
    if rate <= 0:
        return None
    seconds = free_bytes / rate
    return seconds if seconds <= DISK_PROJECTION_HORIZON_SECONDS else None


@pure
def _is_disk_filling_fast(disk: DiskReading) -> bool:
    return disk.seconds_to_full is not None and disk.seconds_to_full <= DISK_FILLING_FAST_SECONDS


@pure
def _is_disk_warned(disk: DiskReading, level_gb: float, is_raised: bool) -> bool:
    level_bytes = level_gb * GIB
    if disk.free_bytes <= level_bytes or _is_disk_filling_fast(disk):
        return True
    return is_raised and disk.free_bytes <= level_bytes * DISK_CLEAR_FACTOR


@pure
def _is_share_warned(percent: float, level: int, is_raised: bool) -> bool:
    return percent >= level or (is_raised and percent > level - WARNING_CLEAR_MARGIN_PERCENT)


@pure
def next_warnings(
    raised: frozenset[GaugeResource], readings: DeviceReadings, settings: DeviceResourceSettings
) -> frozenset[GaugeResource]:
    """The warnings raised after this reading (see the module docstring for when one is raised and cleared)."""
    result: set[GaugeResource] = set()
    disk_level = settings.disk_warning_free_gb
    if disk_level is not None:
        is_raised = GaugeResource.DISK in raised
        if is_raised if readings.disk is None else _is_disk_warned(readings.disk, disk_level, is_raised):
            result.add(GaugeResource.DISK)
    memory_level = settings.memory_warning_percent
    if memory_level is not None:
        is_raised = GaugeResource.MEMORY in raised
        if (
            is_raised
            if readings.memory is None
            else _is_share_warned(readings.memory.percent, memory_level, is_raised)
        ):
            result.add(GaugeResource.MEMORY)
    cpu_level = settings.cpu_warning_percent
    if cpu_level is not None:
        is_raised = GaugeResource.CPU in raised
        if is_raised if readings.cpu is None else _is_share_warned(readings.cpu.recent_percent, cpu_level, is_raised):
            result.add(GaugeResource.CPU)
    return frozenset(result)


@pure
def describe_alert(
    resource: GaugeResource, readings: DeviceReadings, settings: DeviceResourceSettings
) -> ResourceAlert:
    """A raised warning in words: what is wrong, and the figures behind it."""
    match resource:
        case GaugeResource.DISK:
            disk = readings.disk
            if disk is None:
                return ResourceAlert(
                    resource=resource,
                    severity=AlertSeverity.WARNING,
                    headline="Disk space is low",
                    detail="The disk cannot be read right now",
                )
            level_bytes = (settings.disk_warning_free_gb or 0.0) * GIB
            is_low = disk.free_bytes <= level_bytes * DISK_CLEAR_FACTOR
            is_critical = disk.free_bytes <= level_bytes * DISK_CRITICAL_FACTOR
            detail = f"{format_gigabytes(disk.free_bytes)} left"
            if disk.seconds_to_full is not None:
                detail += f", about {format_duration(disk.seconds_to_full)} at this rate"
            if is_critical:
                headline = "Disk is almost full"
            elif is_low:
                headline = "Disk space is low"
            else:
                headline = "Disk is filling up fast"
            return ResourceAlert(
                resource=resource,
                severity=AlertSeverity.CRITICAL if is_critical else AlertSeverity.WARNING,
                headline=headline,
                detail=detail,
            )
        case GaugeResource.MEMORY:
            memory = readings.memory
            return ResourceAlert(
                resource=resource,
                severity=AlertSeverity.WARNING,
                headline="Memory is nearly full",
                detail=f"{format_gigabytes(memory.free_bytes)} left of {format_gigabytes(memory.total_bytes)}"
                if memory is not None
                else "Memory cannot be read right now",
            )
        case GaugeResource.CPU:
            cpu = readings.cpu
            return ResourceAlert(
                resource=resource,
                severity=AlertSeverity.WARNING,
                headline="CPU is busy",
                detail=f"{cpu.recent_percent:.0f}% in use over the last two minutes"
                if cpu is not None
                else "No CPU reading right now",
            )


@pure
def alerts_for(
    warnings: frozenset[GaugeResource], readings: DeviceReadings, settings: DeviceResourceSettings
) -> tuple[ResourceAlert, ...]:
    """The raised warnings in words, critical ones first, then disk, memory and CPU."""
    alerts = [
        describe_alert(resource, readings, settings)
        for resource in (GaugeResource.DISK, GaugeResource.MEMORY, GaugeResource.CPU)
        if resource in warnings
    ]
    alerts.sort(key=lambda alert: alert.severity != AlertSeverity.CRITICAL)
    return tuple(alerts)


@pure
def is_push_due(previous: DeviceResourceView | None, current: DeviceResourceView) -> bool:
    """Whether the windows draw ``current`` differently from the view they were last sent: the titlebar draws only
    the warnings. The settings section, which shows the readings, fetches the view while it is open."""
    if previous is None:
        return True
    return previous.settings != current.settings or [
        (alert.resource, alert.severity, alert.headline) for alert in previous.alerts
    ] != [(alert.resource, alert.severity, alert.headline) for alert in current.alerts]
