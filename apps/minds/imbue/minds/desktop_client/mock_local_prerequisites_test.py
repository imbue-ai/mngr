from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from imbue.minds.desktop_client.local_prerequisites import CommandProbeResult
from imbue.minds.desktop_client.local_prerequisites import DeviceAccess
from imbue.minds.desktop_client.local_prerequisites import HostProbeInterface


class FakeHostProbe(HostProbeInterface):
    """A machine described by configuration: which executables exist, what commands answer, which devices open."""

    system: str = Field(default="Linux", description="What platform.system() reports")
    machine: str = Field(default="x86_64", description="What platform.machine() reports")
    executables: frozenset[str] = Field(default=frozenset(), description="Names that resolve on PATH")
    result_by_command: dict[str, CommandProbeResult | None] = Field(
        default_factory=dict, description="Outcome per space-joined command; a missing entry is a launch failure"
    )
    access_by_device: dict[str, DeviceAccess] = Field(
        default_factory=dict, description="How each device path answers; a missing entry is an absent node"
    )
    commands_run: list[str] = Field(default_factory=list, description="Every command run, space-joined, in order")

    def platform_system(self) -> str:
        return self.system

    def platform_machine(self) -> str:
        return self.machine

    def which(self, executable_name: str) -> Path | None:
        return Path("/usr/bin") / executable_name if executable_name in self.executables else None

    def run(self, command: Sequence[str]) -> CommandProbeResult | None:
        joined = " ".join(command)
        self.commands_run.append(joined)
        return self.result_by_command.get(joined)

    def device_access(self, device_path: Path) -> DeviceAccess:
        return self.access_by_device.get(str(device_path), DeviceAccess.ABSENT)
