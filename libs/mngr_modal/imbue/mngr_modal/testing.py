"""Test utilities for mngr_modal.

Non-fixture helpers for creating test objects. Fixtures that use these
helpers live in conftest.py.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import SnapshotRecord
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_modal.backend import STATE_VOLUME_SUFFIX
from imbue.mngr_modal.config import ModalMode
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.mngr_modal.instance import HostRecord
from imbue.mngr_modal.instance import ModalProviderApp
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.mngr_modal.instance import SandboxConfig
from imbue.mngr_modal.instance import TAG_HOST_ID
from imbue.mngr_modal.instance import TAG_HOST_NAME
from imbue.mngr_modal.instance import TAG_USER_PREFIX
from imbue.modal_proxy.interface import SandboxInterface
from imbue.modal_proxy.testing import TestingModalInterface

_DEFAULT_SANDBOX_CONFIG = SandboxConfig()


def make_testing_modal_interface(tmp_path: Path, cg: ConcurrencyGroup) -> TestingModalInterface:
    """Create a TestingModalInterface rooted in a temp directory."""
    root = tmp_path / "modal_testing"
    root.mkdir(parents=True, exist_ok=True)
    return TestingModalInterface(root_dir=root, concurrency_group=cg)


def make_testing_provider(
    mngr_ctx: MngrContext,
    modal_interface: TestingModalInterface,
    app_name: str = "test-app",
    is_persistent: bool = False,
    is_snapshotted_after_create: bool = False,
    is_host_volume_created: bool = True,
) -> ModalProviderInstance:
    """Create a ModalProviderInstance backed by TestingModalInterface."""
    environment_name = f"{mngr_ctx.config.prefix}test-user"

    app = modal_interface.app_lookup(app_name, create_if_missing=True, environment_name=environment_name)
    volume_name = f"{app_name}{STATE_VOLUME_SUFFIX}"
    volume = modal_interface.volume_from_name(
        volume_name,
        create_if_missing=True,
        environment_name=environment_name,
    )

    config = ModalProviderConfig(
        mode=ModalMode.TESTING,
        app_name=app_name,
        host_dir=mngr_ctx.config.default_host_dir,
        default_sandbox_timeout=300,
        default_cpu=0.5,
        default_memory=0.5,
        is_persistent=is_persistent,
        is_snapshotted_after_create=is_snapshotted_after_create,
        is_host_volume_created=is_host_volume_created,
        ssh_connect_timeout=5.0,
    )

    modal_app = ModalProviderApp(
        app_name=app_name,
        environment_name=environment_name,
        app=app,
        volume=volume,
        modal_interface=modal_interface,
        close_callback=lambda: None,
        get_output_callback=lambda: "",
    )

    return ModalProviderInstance(
        name=ProviderInstanceName("modal-test"),
        host_dir=mngr_ctx.config.default_host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        modal_app=modal_app,
    )


def make_snapshot(snap_id: str = "snap-1", name: str = "s1") -> SnapshotRecord:
    """Create a SnapshotRecord for testing."""
    return SnapshotRecord(id=snap_id, name=name, created_at=datetime.now(timezone.utc).isoformat())


def make_host_record(
    host_id: HostId | None = None,
    host_name: str = "test-host",
    snapshots: list[SnapshotRecord] | None = None,
    failure_reason: str | None = None,
    user_tags: dict[str, str] | None = None,
    config: SandboxConfig | None = _DEFAULT_SANDBOX_CONFIG,
    ssh_host: str | None = "127.0.0.1",
    ssh_port: int | None = 22222,
    ssh_host_public_key: str | None = "ssh-ed25519 AAAA...",
) -> HostRecord:
    """Create a HostRecord for testing."""
    if host_id is None:
        host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name=host_name,
        user_tags=user_tags or {},
        snapshots=snapshots or [],
        failure_reason=failure_reason,
        created_at=now,
        updated_at=now,
    )
    return HostRecord(
        certified_host_data=certified_data,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_host_public_key=ssh_host_public_key,
        config=config,
    )


def make_sandbox_with_tags(
    modal_interface: TestingModalInterface,
    host_id: HostId,
    host_name: str,
    user_tags: dict[str, str] | None = None,
) -> SandboxInterface:
    """Create a testing sandbox with mngr tags set."""
    image = modal_interface.image_debian_slim()
    app = list(modal_interface._apps.values())[0]
    sandbox = modal_interface.sandbox_create(
        image=image,
        app=app,
        timeout=300,
        cpu=1.0,
        memory=1024,
    )
    tags: dict[str, str] = {
        TAG_HOST_ID: str(host_id),
        TAG_HOST_NAME: host_name,
    }
    if user_tags:
        for key, value in user_tags.items():
            tags[TAG_USER_PREFIX + key] = value
    sandbox.set_tags(tags)
    return sandbox


def setup_host_with_sandbox(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
    host_name: str,
    user_tags: dict[str, str] | None = None,
) -> tuple[HostId, HostRecord, SandboxInterface]:
    """Common setup: create a host record, sandbox with tags, and cache both.

    Returns (host_id, record, sandbox). The host cache is populated with an
    OfflineHost so that get_host() returns it without SSH.
    """
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name=host_name, user_tags=user_tags)
    testing_provider._write_host_record(record)
    sandbox = make_sandbox_with_tags(testing_modal, host_id, host_name, user_tags=user_tags)
    testing_provider._cache_sandbox(host_id, HostName(host_name), sandbox)
    offline = testing_provider._create_host_from_host_record(record)
    testing_provider._host_by_id_cache[host_id] = offline
    return host_id, record, sandbox
