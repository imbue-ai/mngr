"""Test utilities for mngr_modal.

Non-fixture helpers for creating test objects. Fixtures that use these
helpers live in conftest.py.
"""

import json
import os
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import SnapshotRecord
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UserId
from imbue.mngr.utils.logging import get_default_cli_events_log_dir
from imbue.mngr.utils.testing import ModalSubprocessTestEnv
from imbue.mngr.utils.testing import read_shared_modal_env_name
from imbue.mngr_modal.backend import ModalProviderBackend
from imbue.mngr_modal.backend import derive_modal_app_name
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.mngr_modal.instance import HostRecord
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.mngr_modal.instance import SandboxConfig
from imbue.mngr_modal.instance import TAG_HOST_ID
from imbue.mngr_modal.instance import TAG_HOST_NAME
from imbue.mngr_modal.instance import TAG_USER_PREFIX
from imbue.mngr_modal.plugin import MODAL_BACKEND_NAME
from imbue.modal_proxy.interface import SandboxInterface
from imbue.modal_proxy.testing import FakeModalInterface

# Release-test opt-in flag. Mirrors ``AWS_RELEASE_TESTS_OPT_IN`` in mngr_aws:
# the Modal release trip only runs when this is set, on top of the Modal
# credential check. Read once at import time so the test module and any
# gating helper observe the same value.
MODAL_RELEASE_TESTS_OPT_IN: Final[bool] = os.environ.get("MNGR_MODAL_RELEASE_TESTS") == "1"

_DEFAULT_SANDBOX_CONFIG = SandboxConfig()

_MNGR_EVENT_LOG_NAME: Final[str] = "events.jsonl"


def _format_logged_event(raw_event: str) -> str:
    """One recorded event as ``timestamp LEVEL message context``.

    An event that does not parse is passed through verbatim: it is still the
    only account there is of what the subprocess was doing.
    """
    try:
        event: dict[str, Any] = json.loads(raw_event)
    except json.JSONDecodeError:
        return raw_event
    context = event.get("extra", "")
    return f"{event['timestamp']} {event['level']} {event['message']} {context}".rstrip()


class MngrSubprocessTimeoutError(Exception):
    """A ``mngr`` command in a Modal test overran its timeout, with the steps it had reached."""


def _format_steps_of_killed_mngr_subprocess(event_log_path: Path, start_offset: int) -> str:
    """The steps a ``mngr`` subprocess had reached, read back from its own event log.

    A ``mngr`` subprocess killed before it returns takes its captured stdout and
    stderr with it, whichever budget ran out first: ``subprocess.run`` hands the
    partial capture to a ``TimeoutExpired`` that says only how long it waited,
    and pytest-timeout's signal abandons the call mid-wait. The event log is on
    disk and flushed per line, so it still holds every step the subprocess had
    entered, each with the time it started.
    """
    if not event_log_path.exists():
        return f"Found no mngr event log at {event_log_path}, so there is no step timeline to report."
    with event_log_path.open("rb") as event_log:
        event_log.seek(start_offset)
        appended_events = event_log.read().decode("utf-8", errors="replace")
    steps = "\n".join(_format_logged_event(raw_event) for raw_event in appended_events.splitlines())
    return f"Steps from the killed mngr subprocess's event log {event_log_path}:\n{steps}"


def run_mngr_against_modal(
    *args: str,
    modal_subprocess_env: ModalSubprocessTestEnv,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run one ``mngr`` command against real Modal, naming the step it reached if it is killed.

    Use this for every ``mngr`` command a Modal acceptance test runs. Modal
    bring-up has several steps that can each outlast the enclosing test on their
    own, so which one was running is the first thing such a failure has to say --
    and a plain ``subprocess.run`` cannot say it (MIND-487).

    A command that overruns its own timeout is reported by raising, so the steps
    land in the failure's message: that is what reaches junit, and so the flake
    sweeps and the GitHub annotation, neither of which reads a test's captured
    output. The raised message keeps ``TimeoutExpired``'s own wording as its
    first line, which is what those sweeps key their display header on. When
    something else does the killing there is no message to carry, so the steps
    go to the log instead.
    """
    event_log_path = get_default_cli_events_log_dir(modal_subprocess_env.host_dir) / _MNGR_EVENT_LOG_NAME
    start_offset = event_log_path.stat().st_size if event_log_path.exists() else 0
    is_accounted_for = False
    try:
        result = subprocess.run(
            ["uv", "run", "mngr", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=modal_subprocess_env.env,
        )
        is_accounted_for = True
        return result
    except subprocess.TimeoutExpired as timeout_error:
        is_accounted_for = True
        steps = _format_steps_of_killed_mngr_subprocess(event_log_path, start_offset)
        raise MngrSubprocessTimeoutError(f"{timeout_error}\n{steps}") from timeout_error
    finally:
        if not is_accounted_for:
            logger.info("{}", _format_steps_of_killed_mngr_subprocess(event_log_path, start_offset))


def make_testing_modal_interface(tmp_path: Path, cg: ConcurrencyGroup) -> FakeModalInterface:
    """Create a FakeModalInterface rooted in a temp directory."""
    root = tmp_path / "modal_testing"
    root.mkdir(parents=True, exist_ok=True)
    return FakeModalInterface(root_dir=root, concurrency_group=cg)


def make_testing_provider(
    mngr_ctx: MngrContext,
    modal_interface: FakeModalInterface,
    app_name: str = "test-app",
    is_persistent: bool = False,
    is_snapshotted_after_create: bool = False,
    is_host_volume_created: bool = True,
) -> ModalProviderInstance:
    """Create a ``ModalProviderInstance`` backed by ``FakeModalInterface``.

    Calls ``ModalProviderBackend._construct_modal_provider`` (the shared
    factory body that production also uses) with the fake injected.
    ``user_id="test-user"`` pins the environment-name format to
    ``f"{prefix}test-user"``.
    """
    config = ModalProviderConfig(
        app_name=app_name,
        user_id=UserId("test-user"),
        host_dir=mngr_ctx.config.default_host_dir,
        default_sandbox_timeout=300,
        default_cpu=0.5,
        default_memory=0.5,
        is_persistent=is_persistent,
        is_snapshotted_after_create=is_snapshotted_after_create,
        is_host_volume_created=is_host_volume_created,
        ssh_connect_timeout=5.0,
    )
    instance = ModalProviderBackend._construct_modal_provider(
        ProviderInstanceName("modal-test"),
        config,
        mngr_ctx,
        modal_interface,
    )
    if not isinstance(instance, ModalProviderInstance):
        raise ConfigStructureError(f"Expected ModalProviderInstance, got {type(instance).__name__}")
    return instance


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
    modal_interface: FakeModalInterface,
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
    testing_modal: FakeModalInterface,
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


class SharedModalTestRunNames(FrozenModel):
    """The Modal app and environment that one offload run's hosts all land in.

    A shared-env run points every fanned-out sandbox at one Modal environment,
    and every ``mngr create`` subprocess in it at one app -- the default app of
    the ``modal`` provider instance under the run's prefix.
    """

    app_name: str = Field(description="The Modal app every create in the run shares")
    environment_name: str = Field(description="The Modal environment every create in the run shares")


def get_shared_modal_test_run_names() -> SharedModalTestRunNames | None:
    """Name the run's shared Modal app and environment, or None outside shared-env mode.

    Lets a setup step that runs before the fan-out address the same app and
    environment the run's creates will.
    """
    shared = read_shared_modal_env_name()
    if shared is None:
        return None
    timestamp_name, user_id_suffix = shared
    return SharedModalTestRunNames(
        app_name=derive_modal_app_name(
            f"{timestamp_name}-", ProviderInstanceName(str(MODAL_BACKEND_NAME)), configured_app_name=None
        ),
        environment_name=f"{timestamp_name}-{user_id_suffix}",
    )
