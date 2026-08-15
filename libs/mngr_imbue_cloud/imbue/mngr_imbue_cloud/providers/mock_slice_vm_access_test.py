"""Shared in-memory slice-access mock for adoption unit tests (imported explicitly, defines no tests)."""

from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr_imbue_cloud.errors import AdoptionError
from imbue.mngr_imbue_cloud.interfaces import SliceReconcilerState
from imbue.mngr_imbue_cloud.interfaces import SliceVmAccessInterface


class MockSliceVmAccess(SliceVmAccessInterface):
    """In-memory model of one slice: two sshd endpoints, a VM root authorized_keys, a container copy.

    Mirrors the real host's observable behavior at the operation level: installs
    change which key an endpoint serves, the reconciler asserts the desired file
    onto the live one, and authentication succeeds iff the key's public half is
    in the endpoint's authorized_keys. ``operations_until_failure`` injects a
    crash after that many mutating operations, for crash-at-each-step tests.
    """

    vm_port: int = Field(description="Port modeling the VM-root sshd endpoint")
    container_port: int = Field(description="Port modeling the container sshd endpoint")
    served_key_by_port: dict[int, str] = Field(default_factory=dict, description="Host key each endpoint serves")
    vm_authorized_keys: str | None = Field(default=None, description="Live VM root authorized_keys content")
    container_authorized_keys: str = Field(default="", description="Container root authorized_keys content")
    desired_authorized_keys: str | None = Field(default=None, description="The desired-state file, once installed")
    is_unit_enabled: bool = Field(default=False, description="Whether the reconciler unit is installed + enabled")
    is_authentication_always_failing: bool = Field(
        default=False, description="Force can_authenticate to fail, modeling an endpoint that rejects every key"
    )
    operations_until_failure: int | None = Field(
        default=None, description="Raise AdoptionError after this many mutating operations (None: never)"
    )

    _mutation_count: int = PrivateAttr(default=0)
    _call_count: int = PrivateAttr(default=0)

    @property
    def call_count(self) -> int:
        return self._call_count

    def _record_call(self) -> None:
        self._call_count += 1

    def _record_mutation(self) -> None:
        self._record_call()
        self._mutation_count += 1
        if self.operations_until_failure is not None and self._mutation_count > self.operations_until_failure:
            raise AdoptionError("injected crash for testing")

    def read_vm_root_authorized_keys(self) -> str | None:
        self._record_call()
        return self.vm_authorized_keys

    def install_reconciler(self, desired_authorized_keys: str) -> None:
        self._record_mutation()
        self.desired_authorized_keys = desired_authorized_keys
        self.vm_authorized_keys = desired_authorized_keys
        self.is_unit_enabled = True

    def read_reconciler_state(self) -> SliceReconcilerState:
        self._record_call()
        return SliceReconcilerState(
            is_unit_enabled=self.is_unit_enabled,
            desired_authorized_keys=self.desired_authorized_keys,
            is_live_matching_desired=self.desired_authorized_keys is not None
            and self.vm_authorized_keys == self.desired_authorized_keys,
        )

    def install_vm_host_key(self, private_key_pem: str, public_key: str) -> None:
        self._record_mutation()
        self.served_key_by_port[self.vm_port] = public_key.strip()

    def install_container_host_key(self, private_key_pem: str, public_key: str) -> None:
        self._record_mutation()
        self.served_key_by_port[self.container_port] = public_key.strip()

    def append_container_authorized_key(self, public_key: str) -> None:
        self._record_mutation()
        lines = [line for line in self.container_authorized_keys.splitlines() if line.strip()]
        if public_key.strip() not in lines:
            lines.append(public_key.strip())
        self.container_authorized_keys = "".join(f"{line}\n" for line in lines)

    def remove_container_authorized_key(self, public_key: str) -> None:
        self._record_mutation()
        lines = [
            line
            for line in self.container_authorized_keys.splitlines()
            if line.strip() and line.strip() != public_key.strip()
        ]
        self.container_authorized_keys = "".join(f"{line}\n" for line in lines)

    def is_endpoint_serving_host_key(self, port: int, public_key: str) -> bool:
        self._record_call()
        return self.served_key_by_port.get(port) == public_key.strip()

    def can_authenticate(self, port: int, private_key_path: Path) -> bool:
        self._record_call()
        if self.is_authentication_always_failing:
            return False
        public_key_path = private_key_path.with_name(private_key_path.name + ".pub")
        if not public_key_path.exists():
            return False
        public_key = public_key_path.read_text().strip()
        if port == self.vm_port:
            authorized_content = self.vm_authorized_keys or ""
        elif port == self.container_port:
            authorized_content = self.container_authorized_keys
        else:
            return False
        return public_key in {line.strip() for line in authorized_content.splitlines()}
