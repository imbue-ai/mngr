"""A device resource reader that answers with whatever a test set."""

from pydantic import Field

from imbue.minds.desktop_client.device_resource_monitor import DeviceResourceReaderInterface
from imbue.minds.desktop_client.device_resources import DeviceReadings
from imbue.minds.desktop_client.device_resources import NO_DEVICE_READINGS


class FakeDeviceResourceReader(DeviceResourceReaderInterface):
    """Answers each read with the readings a test set."""

    device: DeviceReadings = Field(default=NO_DEVICE_READINGS, description="What read_device answers")

    def read_device(self) -> DeviceReadings:
        return self.device
