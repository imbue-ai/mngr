from imbue.mngr.primitives import HostName
from imbue.mngr_smolvm.smolvm_cli import smolvm_machine_name


def test_smolvm_machine_name_applies_prefix() -> None:
    assert smolvm_machine_name(HostName("my-host"), "mngr-") == "mngr-my-host"


def test_smolvm_machine_name_custom_prefix() -> None:
    assert smolvm_machine_name(HostName("h"), "custom-") == "custom-h"
