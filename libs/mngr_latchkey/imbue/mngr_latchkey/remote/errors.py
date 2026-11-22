from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import LatchkeyError


class RemoteGatewayError(LatchkeyError, RuntimeError):
    """Raised when an exchange with a remote machine's latchkey gateway fails."""


class MachineNewerError(RemoteGatewayError):
    """Raised when a machine's software is of a newer generation than this build manages.

    A build leaves such a machine alone: it installs nothing there and runs
    none of its commands. Nothing a retry fixes, and nothing wrong with the
    machine: the remedy is a newer build on this computer.
    """

    def __init__(self, machine_name: str) -> None:
        self.machine_name = machine_name
        super().__init__(
            f"Machine {machine_name} was set up by a newer version of mngr than this one, which leaves it alone: "
            "update mngr, or the app that runs it, on this computer to manage the machine from here"
        )


@pure
def is_caused_by_newer_machine(error: BaseException) -> bool:
    """Whether ``error``, or any error it was explicitly raised from, is a :class:`MachineNewerError`."""
    link: BaseException | None = error
    while link is not None:
        if isinstance(link, MachineNewerError):
            return True
        link = link.__cause__
    return False


class SentToMachine(UpperCaseStrEnum):
    """A part of what one machine command hands the machine to hold or act on."""

    ENCRYPTION_KEY = auto()
    LISTEN_PASSWORD = auto()
    CONFIG = auto()
    CREDENTIAL_MERGE = auto()
    CREDENTIAL_CLEAR = auto()
    CREDENTIAL_STORE_ABANDONMENT = auto()
    DESKTOP_EGRESS_RULES = auto()
    PERMISSIONS = auto()
    PERMISSIONS_FORMAT_VERSION = auto()
    GATEWAY_LISTEN_HOST = auto()
    TUNNEL = auto()
    GATEWAY_RESTART = auto()
    DEVICE_RECORD = auto()
    PERMISSION_REQUEST_WITHDRAWAL = auto()


class ReceivedFromMachine(UpperCaseStrEnum):
    """A part of what one machine command brings back from the machine."""

    ENCRYPTION_KEY = auto()
    LISTEN_PASSWORD = auto()
    CONFIG = auto()
    PERMISSIONS = auto()
    PERMISSIONS_FORMAT_VERSION = auto()
    DESKTOP_EGRESS_RULES = auto()
    CREDENTIAL_STORE = auto()
    CONTAINER_EXTRA_HOSTS = auto()
    FILED_PERMISSION_REQUESTS = auto()


@pure
def describe_machine_transfer(sent: frozenset[SentToMachine], expected: frozenset[ReceivedFromMachine]) -> str:
    """What one machine command does, as the verb phrase before the machine it does it to: ``send X to``."""
    sent_names = ", ".join(part for part in SentToMachine if part in sent)
    expected_names = ", ".join(part for part in ReceivedFromMachine if part in expected)
    if sent and expected:
        return f"send {sent_names} to and read {expected_names} from"
    if sent:
        return f"send {sent_names} to"
    if expected:
        return f"read {expected_names} from"
    return "run a command carrying nothing on"


class MachineTransferError(RemoteGatewayError):
    """Raised when one machine command fails: what it was sending the machine, and what it expected back.

    Whatever failed the command -- a connection that could not be made, the
    machine refusing or failing the command, an answer this build cannot
    read -- is the ``__cause__`` when it is an error of its own, so a check
    that walks the chain (whether discovery should retry, say) still sees it.
    """

    def __init__(
        self,
        host_id: HostId,
        vps_name: str,
        sent: frozenset[SentToMachine],
        expected: frozenset[ReceivedFromMachine],
        reason: str,
    ) -> None:
        self.host_id = host_id
        self.sent = sent
        self.expected = expected
        super().__init__(
            f"Failed to {describe_machine_transfer(sent, expected)} VPS {vps_name} for host {host_id}: {reason}"
        )
