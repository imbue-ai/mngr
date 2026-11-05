"""Shared in-memory slice-access mock for adoption unit tests (imported explicitly, defines no tests)."""

from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr_imbue_cloud.errors import AdoptionError
from imbue.mngr_imbue_cloud.interfaces import SliceReconcilerState
from imbue.mngr_imbue_cloud.interfaces import SliceVmAccessInterface
from imbue.mngr_imbue_cloud.providers.adoption import expected_reconciler_content_hash


class MockSliceVmAccess(SliceVmAccessInterface):
    """In-memory model of one slice: two sshd endpoints, a VM root authorized_keys, a container copy.

    Mirrors the real host's observable behavior at the operation level: installs
    change which key an endpoint serves, the reconciler asserts the desired file
    onto the live one, and authentication succeeds iff the key's public half is
    in the endpoint's authorized_keys. ``operations_until_failure`` injects a
    crash after that many mutating operations, for crash-at-each-step tests, and
    ``unanswered_probe_count_after_install`` models an sshd that takes a while to
    come back from the reload a host-key install triggers.
    """

    vm_port: int = Field(description="Port modeling the VM-root sshd endpoint")
    container_port: int = Field(description="Port modeling the container sshd endpoint")
    served_key_by_port: dict[int, str] = Field(default_factory=dict, description="Host key each endpoint serves")
    vm_authorized_keys: str | None = Field(default=None, description="Live VM root authorized_keys content")
    container_authorized_keys: str = Field(default="", description="Container root authorized_keys content")
    desired_authorized_keys: str | None = Field(default=None, description="The desired-state file, once installed")
    is_unit_enabled: bool = Field(default=False, description="Whether the reconciler unit is installed + enabled")
    installed_content_hash: str | None = Field(
        default=None,
        description=(
            "sha256 the installed reconciler unit + script would report, set to the current expected hash by "
            "install_reconciler; override to model a VM carrying an older client version's reconciler content"
        ),
    )
    is_authentication_always_failing: bool = Field(
        default=False, description="Force can_authenticate to fail, modeling an endpoint that rejects every key"
    )
    operations_until_failure: int | None = Field(
        default=None, description="Raise AdoptionError after this many mutating operations (None: never)"
    )
    unanswered_probe_count_after_install: int = Field(
        default=0,
        description="How many served-key probes of an endpoint fail after a host-key install, while its sshd reloads",
    )

    _mutation_count: int = PrivateAttr(default=0)
    _operations: list[str] = PrivateAttr(default_factory=list)
    _unanswered_probe_count_by_port: dict[int, int] = PrivateAttr(default_factory=dict)

    @property
    def call_count(self) -> int:
        return len(self._operations)

    @property
    def operations(self) -> tuple[str, ...]:
        """Every operation called so far, in order, by method name."""
        return tuple(self._operations)

    def _record_call(self, operation: str) -> None:
        self._operations.append(operation)

    def _record_mutation(self, operation: str) -> None:
        self._record_call(operation)
        self._mutation_count += 1
        if self.operations_until_failure is not None and self._mutation_count > self.operations_until_failure:
            raise AdoptionError("injected crash for testing")

    def read_vm_root_authorized_keys(self) -> str | None:
        self._record_call("read_vm_root_authorized_keys")
        return self.vm_authorized_keys

    def install_reconciler(self, desired_authorized_keys: str) -> None:
        self._record_mutation("install_reconciler")
        self.desired_authorized_keys = desired_authorized_keys
        self.vm_authorized_keys = desired_authorized_keys
        self.is_unit_enabled = True
        self.installed_content_hash = expected_reconciler_content_hash()

    def read_reconciler_state(self) -> SliceReconcilerState:
        self._record_call("read_reconciler_state")
        return SliceReconcilerState(
            is_unit_enabled=self.is_unit_enabled,
            desired_authorized_keys=self.desired_authorized_keys,
            is_live_matching_desired=self.desired_authorized_keys is not None
            and self.vm_authorized_keys == self.desired_authorized_keys,
            installed_content_hash=self.installed_content_hash,
        )

    def install_vm_host_key(self, private_key_pem: str, public_key: str) -> None:
        self._record_mutation("install_vm_host_key")
        self._serve_installed_host_key(self.vm_port, public_key)

    def install_container_host_key(self, private_key_pem: str, public_key: str) -> None:
        self._record_mutation("install_container_host_key")
        self._serve_installed_host_key(self.container_port, public_key)

    def _serve_installed_host_key(self, port: int, public_key: str) -> None:
        self.served_key_by_port[port] = public_key.strip()
        self._unanswered_probe_count_by_port[port] = self.unanswered_probe_count_after_install

    def append_container_authorized_key(self, public_key: str) -> None:
        self._record_mutation("append_container_authorized_key")
        lines = [line for line in self.container_authorized_keys.splitlines() if line.strip()]
        if public_key.strip() not in lines:
            lines.append(public_key.strip())
        self.container_authorized_keys = "".join(f"{line}\n" for line in lines)

    def remove_container_authorized_key(self, public_key: str) -> None:
        self._record_mutation("remove_container_authorized_key")
        lines = [
            line
            for line in self.container_authorized_keys.splitlines()
            if line.strip() and line.strip() != public_key.strip()
        ]
        self.container_authorized_keys = "".join(f"{line}\n" for line in lines)

    def is_endpoint_serving_host_key(self, port: int, public_key: str) -> bool:
        self._record_call("is_endpoint_serving_host_key")
        unanswered_probe_count = self._unanswered_probe_count_by_port.get(port, 0)
        if unanswered_probe_count > 0:
            self._unanswered_probe_count_by_port[port] = unanswered_probe_count - 1
            return False
        return self.served_key_by_port.get(port) == public_key.strip()

    def can_authenticate(self, port: int, private_key_path: Path) -> bool:
        self._record_call("can_authenticate")
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
