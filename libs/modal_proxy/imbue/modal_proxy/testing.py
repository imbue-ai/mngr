# Testing implementation of ModalInterface that fakes Modal locally.
#
# Volumes are backed by real directories on disk. Sandboxes run commands
# via ConcurrencyGroup (which handles process tracking and cleanup).
# Images are lightweight no-ops. Apps and environments are thin metadata.

import contextlib
import shutil
import uuid
from collections.abc import Generator
from contextlib import AbstractContextManager
from io import StringIO
from pathlib import Path
from typing import Final
from typing import Mapping
from typing import Sequence

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.modal_proxy.data_types import FileEntry
from imbue.modal_proxy.data_types import FileEntryType
from imbue.modal_proxy.data_types import StreamType
from imbue.modal_proxy.data_types import TunnelInfo
from imbue.modal_proxy.errors import ModalProxyConnectionError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyImageBuildError
from imbue.modal_proxy.errors import ModalProxyNotFoundError
from imbue.modal_proxy.interface import AppInterface
from imbue.modal_proxy.interface import ExecOutput
from imbue.modal_proxy.interface import ExecProcess
from imbue.modal_proxy.interface import FunctionInterface
from imbue.modal_proxy.interface import ImageInterface
from imbue.modal_proxy.interface import ModalInterface
from imbue.modal_proxy.interface import SandboxInterface
from imbue.modal_proxy.interface import SecretInterface
from imbue.modal_proxy.interface import VolumeInterface
from imbue.modal_proxy.log_utils import ModalLoguruWriter

# Object implementations


class FakeExecOutput(ExecOutput):
    """Exec output backed by a completed process result."""

    output_text: str = Field(default="", description="The captured stdout text")

    def read(self) -> str:
        return self.output_text


class FakeExecProcess(ExecProcess):
    """Exec process backed by a ConcurrencyGroup-managed process, or by an already-known outcome."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    completed_output: str = Field(default="", description="Stdout of a command that already finished")
    completed_exit_code: int = Field(default=0, description="Exit code of a command that already finished")
    _running_process: RunningProcess | None = PrivateAttr(default=None)

    def get_stdout(self) -> ExecOutput:
        if self._running_process is not None:
            return FakeExecOutput(output_text=self._running_process.read_stdout())
        return FakeExecOutput(output_text=self.completed_output)

    def wait(self) -> int:
        if self._running_process is not None:
            return self._running_process.wait()
        return self.completed_exit_code


class FakeSecret(SecretInterface):
    """In-memory secret holding key-value pairs."""

    values: dict[str, str | None] = Field(default_factory=dict, description="Secret key-value pairs")


class FakeFunction(FunctionInterface):
    """Testing function with a configurable web URL."""

    url: str | None = Field(default=None, description="The web URL for this function")

    def get_web_url(self) -> str | None:
        return self.url


class FakeDeployment(FrozenModel):
    """One `modal deploy` the fake was asked to perform."""

    script_path: Path = Field(description="The script that was deployed")
    app_name: str = Field(description="The app the script was deployed into")
    extra_env: Mapping[str, str] = Field(description="Environment the script was deployed under")


class FakeImage(ImageInterface):
    """Lightweight no-op image for testing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    image_id: str = Field(description="Unique identifier for this image")
    build_failure_message: str | None = Field(
        default=None, description="If set, build() fails with this message instead of succeeding"
    )
    build_logs: str = Field(default="", description="Build output that fetch_build_logs() reports")
    streamed_build_logs: str = Field(
        default="",
        # Kept apart from build_logs so a fake can pose the case this exists to model:
        # Modal streamed nothing, yet still has the log if asked.
        description="Build output that build() streams into streaming_capture_buffer",
    )
    streaming_capture_buffer: StringIO | None = Field(
        default=None, description="Where build() streams streamed_build_logs, if anywhere"
    )

    def get_object_id(self) -> str:
        return self.image_id

    def apt_install(self, *packages: str) -> "ImageInterface":
        # No-op -- packages are already installed in the test environment
        return self._derive(self.image_id)

    def dockerfile_commands(
        self,
        commands: Sequence[str],
        *,
        context_dir: Path | None = None,
        secrets: Sequence[SecretInterface] = (),
    ) -> "ImageInterface":
        # No-op -- return a new image with a fresh ID to simulate layer caching
        return self._derive(f"img-{uuid.uuid4().hex}")

    def build(self, app: AppInterface) -> None:
        if self.streaming_capture_buffer is not None:
            self.streaming_capture_buffer.write(self.streamed_build_logs)
        if self.build_failure_message is not None:
            raise ModalProxyImageBuildError(self.build_failure_message)

    def fetch_build_logs(self) -> str:
        return self.build_logs

    def _derive(self, image_id: str) -> "FakeImage":
        """Make the next layer.

        Only the last layer of a chain is ever built, so whatever a test
        configured has to reach it.
        """
        return self.model_copy_update(to_update(self.field_ref().image_id, image_id))


class FakeVolume(VolumeInterface):
    """Volume backed by a real directory on disk."""

    root_dir: Path = Field(description="Local directory backing this volume")
    volume_name: str | None = Field(default=None, description="Volume name if known")

    def get_name(self) -> str | None:
        return self.volume_name

    def get_object_id(self) -> str:
        # The backing directory is this volume's identity: deleting it is how the
        # fake expresses a volume that no longer exists.
        if not self.root_dir.is_dir():
            raise ModalProxyNotFoundError(f"Volume not found: {self.volume_name}")
        return f"vo-{self.root_dir.name}"

    def _resolve(self, path: str) -> Path:
        """Resolve a volume path to a local filesystem path."""
        # Strip leading slash and resolve relative to root
        clean = path.lstrip("/")
        resolved = (self.root_dir / clean).resolve()
        # Ensure we don't escape the root directory
        if not str(resolved).startswith(str(self.root_dir.resolve())):
            raise ModalProxyError(f"Path escapes volume root: {path}")
        return resolved

    def listdir(self, path: str) -> list[FileEntry]:
        target = self._resolve(path)
        if not target.exists():
            raise ModalProxyNotFoundError(f"Path not found: {path}")
        if not target.is_dir():
            raise ModalProxyError(f"Not a directory: {path}")
        entries: list[FileEntry] = []
        for child in sorted(target.iterdir()):
            relative = str(child.relative_to(self.root_dir))
            stat = child.stat()
            entries.append(
                FileEntry(
                    path=relative,
                    type=FileEntryType.DIRECTORY if child.is_dir() else FileEntryType.FILE,
                    mtime=stat.st_mtime,
                    size=stat.st_size if child.is_file() else 0,
                )
            )
        return entries

    def read_file(self, path: str) -> bytes:
        target = self._resolve(path)
        if not target.exists():
            raise ModalProxyNotFoundError(f"File not found: {path}")
        if not target.is_file():
            raise ModalProxyError(f"Not a file: {path}")
        return target.read_bytes()

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        target = self._resolve(path)
        if not target.exists():
            raise ModalProxyNotFoundError(f"Path not found: {path}")
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
            else:
                raise ModalProxyError(f"Cannot remove directory without recursive=True: {path}")
        else:
            target.unlink()

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        for path, data in file_contents_by_path.items():
            target = self._resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def reload(self) -> None:
        # No-op -- local filesystem is always up to date
        pass

    def commit(self) -> None:
        # No-op -- writes are immediate on local filesystem
        pass


# Exit code a terminated fake sandbox reports from poll(), mirroring the 137
# (128 + SIGKILL) that Modal surfaces for a terminated sandbox.
_FAKE_TERMINATED_EXIT_CODE: Final[int] = 137

# FakeSandbox runs argv on the machine running the tests. mngr's host bring-up
# (build_configure_ssh_command and friends) rewrites the sshd host key, replaces
# root's authorized_keys, and apt-installs packages; run as root, e.g. inside a
# workspace container, that re-keys the real machine and locks its owner out.
# Any command that reaches for these is refused instead of executed.
_HOST_PROVISIONING_MARKERS: Final[tuple[str, ...]] = ("/etc/ssh", "/usr/sbin/sshd", "apt-get")


class FakeSandboxRefusedHostProvisioningError(ModalProxyError):
    """Raised when a FakeSandbox is asked to run mngr's host bring-up on the test machine."""


class FakeSandbox(SandboxInterface):
    """Sandbox that runs commands locally via ConcurrencyGroup.

    Background processes are tracked by the ConcurrencyGroup and cleaned
    up automatically when the sandbox is terminated or the CG exits.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    sandbox_id: str = Field(description="Unique identifier for this sandbox")
    _tags: dict[str, str] = PrivateAttr(default_factory=dict)
    _is_terminated: bool = PrivateAttr(default=False)
    _cg: ConcurrencyGroup | None = PrivateAttr(default=None)
    _snapshot_count: int = PrivateAttr(default=0)

    def get_object_id(self) -> str:
        return self.sandbox_id

    def exec(
        self,
        *args: str,
        stdout: StreamType = StreamType.PIPE,
        stderr: StreamType = StreamType.PIPE,
    ) -> ExecProcess:
        if self._is_terminated:
            raise ModalProxyError("Sandbox has been terminated")

        if self._cg is None:
            raise ModalProxyError("Sandbox has no ConcurrencyGroup")

        command_text = " ".join(args)
        if any(marker in command_text for marker in _HOST_PROVISIONING_MARKERS):
            raise FakeSandboxRefusedHostProvisioningError(
                "FakeSandbox runs commands on the test machine and refuses to run host provisioning there "
                f"(command mentions one of {_HOST_PROVISIONING_MARKERS}); drive bring-up against a real "
                f"sandbox, or use a sandbox fake that answers without executing. Command: {command_text[:200]}"
            )

        # Check if this is a "background" command (like sshd -D or nohup)
        # that should not block
        is_background = False
        if args and (
            args[-1] == "&"
            or (len(args) >= 2 and args[0] == "/usr/sbin/sshd" and "-D" in args)
            or (len(args) >= 2 and "nohup" in args[0])
        ):
            is_background = True

        if is_background:
            running = self._cg.run_process_in_background(
                list(args),
                is_checked_by_group=False,
            )
            exec_proc = FakeExecProcess()
            exec_proc._running_process = running
            return exec_proc
        else:
            finished = self._cg.run_process_to_completion(
                list(args),
                timeout=60,
                is_checked_after=False,
            )
            return FakeExecProcess(
                completed_output=finished.stdout,
                completed_exit_code=finished.returncode if finished.returncode is not None else 0,
            )

    def tunnels(self, *, timeout: int = 50) -> dict[int, TunnelInfo]:
        if self._is_terminated:
            raise ModalProxyError("Sandbox has been terminated")
        # Return a fixed tunnel for SSH port 22 -> localhost:22222
        # Tests should set up their own SSH server if needed
        return {22: TunnelInfo(tcp_socket=("127.0.0.1", 22222))}

    def get_tags(self) -> dict[str, str]:
        return dict(self._tags)

    def set_tags(self, tags: Mapping[str, str]) -> None:
        self._tags = dict(tags)

    def snapshot_filesystem(self, timeout: int = 120) -> ImageInterface:
        if self._is_terminated:
            raise ModalProxyError("Sandbox has been terminated")
        self._snapshot_count += 1
        image_id = f"snap-{self.sandbox_id}-{self._snapshot_count}"
        return FakeImage(image_id=image_id)

    def poll(self) -> int | None:
        return _FAKE_TERMINATED_EXIT_CODE if self._is_terminated else None

    def terminate(self) -> None:
        if self._is_terminated:
            return
        self._is_terminated = True
        # Terminate all tracked processes via the ConcurrencyGroup
        if self._cg is not None:
            for process in self._cg.unfinished_processes:
                process.terminate(force_kill_seconds=2.0)


class FakeApp(AppInterface):
    """Lightweight testing app with a generated ID."""

    app_id: str = Field(description="Unique app identifier")
    app_name: str = Field(description="Human-readable app name")

    def get_app_id(self) -> str:
        return self.app_id

    def get_name(self) -> str:
        return self.app_name

    def run(self, *, environment_name: str) -> Generator["AppInterface", None, None]:
        yield self


# Top-level implementation


class FakeModalInterface(ModalInterface):
    """Testing implementation of ModalInterface that fakes Modal locally.

    All state is held in memory and on the local filesystem (for volumes).
    No network calls are made. This implementation is designed for testing
    mngr_modal without requiring Modal credentials or a Modal account.

    Requires a ConcurrencyGroup for process lifecycle management. Sandboxes
    create child ConcurrencyGroups so their processes are tracked and cleaned
    up automatically.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root_dir: Path = Field(description="Root directory for volume storage")
    concurrency_group: ConcurrencyGroup = Field(description="Root ConcurrencyGroup for process management")
    _environments: set[str] = PrivateAttr(default_factory=set)
    _apps: dict[str, FakeApp] = PrivateAttr(default_factory=dict)
    _volumes: dict[str, FakeVolume] = PrivateAttr(default_factory=dict)
    _sandboxes: list[FakeSandbox] = PrivateAttr(default_factory=list)
    _functions: dict[str, FakeFunction] = PrivateAttr(default_factory=dict)
    _deployments: list[FakeDeployment] = PrivateAttr(default_factory=list)

    # Environment

    def environment_create(self, name: str) -> None:
        self._environments.add(name)

    # App

    def app_create(self, name: str) -> AppInterface:
        app_id = f"ap-{uuid.uuid4().hex}"
        app = FakeApp(app_id=app_id, app_name=name)
        self._apps[name] = app
        return app

    def app_lookup(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
    ) -> AppInterface:
        # Check that the environment exists (or auto-create it for convenience)
        if environment_name not in self._environments:
            if create_if_missing:
                self._environments.add(environment_name)
            else:
                raise ModalProxyNotFoundError(f"Environment not found: {environment_name}")
        key = f"{environment_name}/{name}"
        if key in self._apps:
            return self._apps[key]
        if create_if_missing:
            app_id = f"ap-{uuid.uuid4().hex}"
            app = FakeApp(app_id=app_id, app_name=name)
            self._apps[key] = app
            return app
        raise ModalProxyNotFoundError(f"App not found: {name}")

    # Image

    def image_debian_slim(self) -> ImageInterface:
        return FakeImage(image_id=f"img-debian-{uuid.uuid4().hex}")

    def image_from_registry(self, name: str) -> ImageInterface:
        return FakeImage(image_id=f"img-reg-{name.replace(':', '-').replace('/', '-')}-{uuid.uuid4().hex}")

    def image_from_id(self, image_id: str) -> ImageInterface:
        return FakeImage(image_id=image_id)

    # Sandbox

    def sandbox_create(
        self,
        *,
        image: ImageInterface,
        app: AppInterface,
        timeout: int,
        cpu: float,
        memory: int,
        unencrypted_ports: Sequence[int] = (),
        gpu: str | None = None,
        region: str | None = None,
        cidr_allowlist: Sequence[str] | None = None,
        volumes: Mapping[str, VolumeInterface] | None = None,
        experimental_options: Mapping[str, bool] | None = None,
    ) -> SandboxInterface:
        sandbox_id = f"sb-{uuid.uuid4().hex}"
        sandbox = self._build_sandbox(sandbox_id)
        # Create a child ConcurrencyGroup for this sandbox's processes
        child_cg = self.concurrency_group.make_concurrency_group(
            name=f"sandbox-{sandbox_id}",
            exit_timeout_seconds=5.0,
        )
        child_cg.__enter__()
        sandbox._cg = child_cg
        self._sandboxes.append(sandbox)
        return sandbox

    def _build_sandbox(self, sandbox_id: str) -> FakeSandbox:
        """The sandbox object sandbox_create hands out; a subclass overrides this to change how commands are answered."""
        return FakeSandbox(sandbox_id=sandbox_id)

    def sandbox_list(self, *, app_id: str) -> list[SandboxInterface]:
        # Return all non-terminated sandboxes
        return [sb for sb in self._sandboxes if not sb._is_terminated]

    def sandbox_from_id(self, sandbox_id: str) -> SandboxInterface:
        for sb in self._sandboxes:
            if sb.sandbox_id == sandbox_id:
                return sb
        raise ModalProxyNotFoundError(f"Sandbox not found: {sandbox_id}")

    # Volume

    def volume_from_name(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
        version: int | None = None,
    ) -> VolumeInterface:
        key = f"{environment_name}/{name}"
        if key in self._volumes:
            return self._volumes[key]
        if not create_if_missing:
            raise ModalProxyNotFoundError(f"Volume not found: {name}")
        vol_dir = self.root_dir / "volumes" / environment_name / name
        vol_dir.mkdir(parents=True, exist_ok=True)
        volume = FakeVolume(root_dir=vol_dir, volume_name=name)
        self._volumes[key] = volume
        return volume

    def volume_list(self, *, environment_name: str) -> list[VolumeInterface]:
        prefix = f"{environment_name}/"
        return [vol for key, vol in self._volumes.items() if key.startswith(prefix)]

    def volume_delete(self, name: str, *, environment_name: str) -> None:
        key = f"{environment_name}/{name}"
        if key not in self._volumes:
            raise ModalProxyNotFoundError(f"Volume not found: {name}")
        volume = self._volumes.pop(key)
        if volume.root_dir.exists():
            shutil.rmtree(volume.root_dir)

    # Secret

    def secret_from_dict(self, values: Mapping[str, str | None]) -> SecretInterface:
        return FakeSecret(values=dict(values))

    # Function

    def function_from_name(
        self,
        name: str,
        *,
        app_name: str,
        environment_name: str | None = None,
    ) -> FunctionInterface:
        key = f"{app_name}/{name}"
        if key in self._functions:
            return self._functions[key]
        raise ModalProxyNotFoundError(f"Function not found: {name} in app {app_name}")

    def is_function_deployed(
        self,
        name: str,
        *,
        app_name: str,
        environment_name: str | None = None,
    ) -> bool:
        return f"{app_name}/{name}" in self._functions

    def register_deployed_function(self, name: str, *, app_name: str, url: str | None = None) -> None:
        """Publish a function into an app, as a deploy of a script declaring it would.

        Lets a test start from an app that is already carrying a given
        deployment, rather than having to deploy a script that produces it.
        """
        self._functions[f"{app_name}/{name}"] = FakeFunction(url=url)

    # CLI

    def deploy(
        self,
        script_path: Path,
        *,
        app_name: str,
        environment_name: str | None = None,
        extra_env: Mapping[str, str] = {},
    ) -> None:
        self._deployments.append(FakeDeployment(script_path=script_path, app_name=app_name, extra_env=dict(extra_env)))
        # Register a testing function for each deployment so function_from_name works
        # Use a predictable URL pattern
        # Scan the script for function names (look for @app.function patterns)
        try:
            content = script_path.read_text()
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("def ") and "(" in stripped:
                    func_name = stripped[4 : stripped.index("(")]
                    key = f"{app_name}/{func_name}"
                    self._functions[key] = FakeFunction(url=f"https://testing.modal.run/{app_name}/{func_name}")
        except (OSError, ValueError) as e:
            logger.trace("Failed to scan script for function names: {}", e)

    def get_deployments(self) -> Sequence[FakeDeployment]:
        """Every deploy performed against this interface, oldest first."""
        return tuple(self._deployments)

    # Testing helpers

    def cleanup(self) -> None:
        """Terminate all sandboxes and exit their ConcurrencyGroups."""
        for sandbox in self._sandboxes:
            sandbox.terminate()
            if sandbox._cg is not None:
                sandbox._cg.__exit__(None, None, None)
                sandbox._cg = None
        self._sandboxes.clear()

    def get_sandbox_count(self) -> int:
        """Get the number of active (non-terminated) sandboxes."""
        return sum(1 for sb in self._sandboxes if not sb._is_terminated)

    def enable_output_capture(
        self, is_logging_to_loguru: bool = True
    ) -> AbstractContextManager[tuple[StringIO, ModalLoguruWriter | None]]:
        """No-op: nothing to capture without a Modal SDK behind it."""
        return contextlib.nullcontext((StringIO(), None))


# Unreachable Modal

# The Modal SDK's own wording when it cannot open a connection to the control
# plane. Reproduced here so a test double reads exactly like the real thing to
# whatever renders the failure to a user.
MODAL_UNREACHABLE_MESSAGE: Final[str] = "Could not connect to the Modal server."


class UnreachableApp(FakeApp):
    """An app handle whose run context cannot be entered because Modal is unreachable."""

    def run(self, *, environment_name: str) -> Generator[AppInterface, None, None]:
        """Fail on the first ``next()``, which is where the real SDK opens the connection."""
        raise ModalProxyConnectionError(MODAL_UNREACHABLE_MESSAGE)
        yield self


class UnreachableModalInterface(FakeModalInterface):
    """A Modal whose control plane cannot be reached -- the dropped-network case.

    Only the calls that actually cross the network fail: constructing an app
    object is local in the real SDK too, so it succeeds here and the failure
    surfaces when its run context is entered.
    """

    def environment_create(self, name: str) -> None:
        raise ModalProxyConnectionError(MODAL_UNREACHABLE_MESSAGE)

    def app_create(self, name: str) -> AppInterface:
        return UnreachableApp(app_id=f"ap-{uuid.uuid4().hex}", app_name=name)

    def app_lookup(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
    ) -> AppInterface:
        raise ModalProxyConnectionError(MODAL_UNREACHABLE_MESSAGE)
