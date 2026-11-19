"""Reads this computer's resources for the resource warnings and pushes the warnings to every window.

One thread reads this computer's CPU, memory and disk every few seconds. The monitor judges the warnings
(``device_resources.py``) and pushes the view to the windows when the warnings or the levels change, and once a
minute regardless. The settings section, which shows the readings, reads :meth:`DeviceResourceMonitor.view` while
it is open.
"""

import json
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Final

import psutil
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.device_resources import AmountReading
from imbue.minds.desktop_client.device_resources import CPU_WARNING_WINDOW_SECONDS
from imbue.minds.desktop_client.device_resources import CpuReading
from imbue.minds.desktop_client.device_resources import DEFAULT_DEVICE_RESOURCE_SETTINGS
from imbue.minds.desktop_client.device_resources import DISK_RATE_WINDOW_SECONDS
from imbue.minds.desktop_client.device_resources import DeviceReadings
from imbue.minds.desktop_client.device_resources import DeviceResourceSettings
from imbue.minds.desktop_client.device_resources import DeviceResourceView
from imbue.minds.desktop_client.device_resources import DiskReading
from imbue.minds.desktop_client.device_resources import GaugeResource
from imbue.minds.desktop_client.device_resources import NO_DEVICE_READINGS
from imbue.minds.desktop_client.device_resources import alerts_for
from imbue.minds.desktop_client.device_resources import estimate_seconds_to_full
from imbue.minds.desktop_client.device_resources import is_push_due
from imbue.minds.desktop_client.device_resources import next_warnings
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.thread_cleanup import start_mngr_thread

DEVICE_RESOURCE_SETTINGS_FILENAME: Final[str] = "device_resource_warnings.json"
DEVICE_RESOURCE_SETTINGS_FILE_VERSION: Final[int] = 1
SAMPLE_INTERVAL_SECONDS: Final[float] = 5.0
PUSH_HEARTBEAT_SECONDS: Final[float] = 60.0


class DeviceResourceReaderInterface(MutableModel, ABC):
    """Reads this computer's resources."""

    @abstractmethod
    def read_device(self) -> DeviceReadings:
        """This computer's CPU (``recent_percent`` equal to ``percent``; the monitor averages), memory and disk."""


class PsutilDeviceResourceReader(DeviceResourceReaderInterface):
    """Reads this computer with psutil."""

    disk_path: Path = Field(frozen=True, description="A path on the disk Studio keeps its data on")

    _is_cpu_primed: bool = PrivateAttr(default=False)

    def read_device(self) -> DeviceReadings:
        # The first call only starts psutil's measurement; it has nothing to compare against yet.
        cpu_percent = psutil.cpu_percent(interval=None)
        cores = psutil.cpu_count() or 1
        cpu = CpuReading(percent=cpu_percent, recent_percent=cpu_percent, cores=cores) if self._is_cpu_primed else None
        self._is_cpu_primed = True
        memory_info = psutil.virtual_memory()
        memory = AmountReading(
            used_bytes=max(0, memory_info.total - memory_info.available),
            free_bytes=memory_info.available,
            total_bytes=memory_info.total,
        )
        disk: DiskReading | None
        try:
            usage = psutil.disk_usage(str(self.disk_path))
        except OSError as e:
            logger.debug("Could not read the disk at {}: {}", self.disk_path, e)
            disk = None
        else:
            disk = DiskReading(used_bytes=usage.used, free_bytes=usage.free, total_bytes=usage.total)
        return DeviceReadings(cpu=cpu, memory=memory, disk=disk)


class DeviceResourceSettingsStore(MutableModel):
    """Reads and writes the warning levels' file in Studio's data directory."""

    data_dir: Path = Field(frozen=True, description="Studio's data directory")

    @property
    def path(self) -> Path:
        return self.data_dir / DEVICE_RESOURCE_SETTINGS_FILENAME

    def read(self) -> DeviceResourceSettings:
        """The stored settings; the defaults when nothing usable is stored (logged when something unusable is)."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return DEFAULT_DEVICE_RESOURCE_SETTINGS
        try:
            document = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning("Ignoring an unreadable {}: {}", self.path, e)
            return DEFAULT_DEVICE_RESOURCE_SETTINGS
        if not isinstance(document, dict) or document.get("version") != DEVICE_RESOURCE_SETTINGS_FILE_VERSION:
            logger.warning("Ignoring {} with an unknown format", self.path)
            return DEFAULT_DEVICE_RESOURCE_SETTINGS
        try:
            return DeviceResourceSettings.model_validate({k: v for k, v in document.items() if k != "version"})
        except ValidationError as e:
            logger.warning("Ignoring invalid settings in {}: {}", self.path, e)
            return DEFAULT_DEVICE_RESOURCE_SETTINGS

    def write(self, settings: DeviceResourceSettings) -> None:
        document = {"version": DEVICE_RESOURCE_SETTINGS_FILE_VERSION, **settings.model_dump(mode="json")}
        atomic_write(self.path, json.dumps(document, indent=2))


class DeviceResourceMonitor(MutableModel):
    """Takes a reading every ``interval_seconds`` on a thread of its own, keeps the view every window reads, and
    pushes it to the windows when what they draw changes."""

    reader: DeviceResourceReaderInterface = Field(frozen=True, description="Takes the readings")
    settings_store: DeviceResourceSettingsStore = Field(frozen=True, description="Where the settings are kept")
    clock: Callable[[], float] = Field(default=time.monotonic, frozen=True, description="Seconds, monotonic")
    interval_seconds: float = Field(default=SAMPLE_INTERVAL_SECONDS, frozen=True, description="Between readings")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _settings: DeviceResourceSettings | None = PrivateAttr(default=None)
    _readings: DeviceReadings = PrivateAttr(default=NO_DEVICE_READINGS)
    _warnings: frozenset[GaugeResource] = PrivateAttr(default=frozenset())
    _cpu_samples: deque[tuple[float, float]] = PrivateAttr(default_factory=deque)
    _disk_samples: deque[tuple[float, int]] = PrivateAttr(default_factory=deque)
    _pushed: DeviceResourceView | None = PrivateAttr(default=None)
    _pushed_at: float = PrivateAttr(default=0.0)
    _on_push_callbacks: list[Callable[[DeviceResourceView], None]] = PrivateAttr(default_factory=list)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _is_started: bool = PrivateAttr(default=False)

    def add_on_push_callback(self, callback: Callable[[DeviceResourceView], None]) -> None:
        """Register where the view goes when the windows should redraw it."""
        self._on_push_callbacks.append(callback)

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the reading thread. Idempotent."""
        if self._is_started:
            return
        self._is_started = True
        start_mngr_thread(concurrency_group, target=self._run, name="device-resource-monitor", is_checked=False)

    def stop(self) -> None:
        self._stop_event.set()

    def view(self) -> DeviceResourceView:
        with self._lock:
            return self._view_locked()

    def update_settings(self, settings: DeviceResourceSettings) -> DeviceResourceView:
        """Keep the new settings and judge the last reading against them at once, so a changed level shows without
        waiting for the next reading."""
        self.settings_store.write(settings)
        with self._lock:
            self._settings = settings
            self._warnings = next_warnings(self._warnings, self._readings, settings)
            view = self._view_locked()
        self._push(view, is_forced=True)
        return view

    def take_reading(self) -> None:
        """One round: read, judge, and tell the windows. What the thread does every interval."""
        now = self.clock()
        raw = self.reader.read_device()
        with self._lock:
            settings = self._current_settings()
            readings = DeviceReadings(
                cpu=self._with_cpu_average(raw.cpu, now),
                memory=raw.memory,
                disk=self._with_projection(raw.disk, now),
            )
            self._readings = readings
            self._warnings = next_warnings(self._warnings, readings, settings)
            view = self._view_locked()
        self._push(view, is_forced=False)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.take_reading()
            except (OSError, psutil.Error) as e:
                logger.warning("A device resource reading failed: {}", e)
            self._stop_event.wait(timeout=self.interval_seconds)

    def _current_settings(self) -> DeviceResourceSettings:
        if self._settings is None:
            self._settings = self.settings_store.read()
        return self._settings

    def _view_locked(self) -> DeviceResourceView:
        settings = self._current_settings()
        return DeviceResourceView(
            settings=settings,
            readings=self._readings,
            alerts=alerts_for(self._warnings, self._readings, settings),
        )

    def _with_cpu_average(self, cpu: CpuReading | None, now: float) -> CpuReading | None:
        if cpu is None:
            return None
        self._cpu_samples.append((now, cpu.percent))
        while self._cpu_samples and now - self._cpu_samples[0][0] > CPU_WARNING_WINDOW_SECONDS:
            self._cpu_samples.popleft()
        recent = sum(percent for _time, percent in self._cpu_samples) / len(self._cpu_samples)
        return cpu.model_copy_update(to_update(cpu.field_ref().recent_percent, recent))

    def _with_projection(self, disk: DiskReading | None, now: float) -> DiskReading | None:
        if disk is None:
            return None
        self._disk_samples.append((now, disk.free_bytes))
        while self._disk_samples and now - self._disk_samples[0][0] > DISK_RATE_WINDOW_SECONDS:
            self._disk_samples.popleft()
        seconds = estimate_seconds_to_full(tuple(self._disk_samples), disk.free_bytes)
        return disk.model_copy_update(to_update(disk.field_ref().seconds_to_full, seconds))

    def _push(self, view: DeviceResourceView, is_forced: bool) -> None:
        now = self.clock()
        with self._lock:
            is_due = is_forced or is_push_due(self._pushed, view) or now - self._pushed_at >= PUSH_HEARTBEAT_SECONDS
            if not is_due:
                return
            self._pushed = view
            self._pushed_at = now
        for callback in self._on_push_callbacks:
            callback(view)
