# Direct implementation of ModalInterface that wraps the real Modal Python SDK.

import contextlib
import io
import os
import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence

import modal
import modal.exception
from modal.stream_type import StreamType as ModalStreamType
from modal.volume import FileEntryType as ModalFileEntryType
from pydantic import ConfigDict
from pydantic import Field

from imbue.modal_proxy.data_types import FileEntry
from imbue.modal_proxy.data_types import FileEntryType
from imbue.modal_proxy.data_types import StreamType
from imbue.modal_proxy.data_types import TunnelInfo
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyTypeError
from imbue.modal_proxy.interface import AppInterface
from imbue.modal_proxy.interface import ExecOutput
from imbue.modal_proxy.interface import ExecProcess
from imbue.modal_proxy.interface import FunctionInterface
from imbue.modal_proxy.interface import ImageInterface
from imbue.modal_proxy.interface import ModalInterface
from imbue.modal_proxy.interface import SandboxInterface
from imbue.modal_proxy.interface import SecretInterface
from imbue.modal_proxy.interface import VolumeInterface


def _to_modal_stream_type(st: StreamType) -> ModalStreamType:
    """Convert our StreamType to modal's StreamType."""
    match st:
        case StreamType.PIPE:
            return ModalStreamType.PIPE
        case StreamType.DEVNULL:
            return ModalStreamType.DEVNULL
        case _:
            raise ModalProxyError(f"Unsupported StreamType: {st}")


def _to_file_entry_type(modal_type: ModalFileEntryType) -> FileEntryType:
    """Convert modal's FileEntryType to ours."""
    match modal_type:
        case ModalFileEntryType.DIRECTORY:
            return FileEntryType.DIRECTORY
        case _:
            return FileEntryType.FILE


# ---------------------------------------------------------------------------
# Unwrap helpers -- extract the underlying modal object from our wrappers.
# ---------------------------------------------------------------------------


def _unwrap_image(iface: ImageInterface) -> modal.Image:
    """Extract the modal.Image from a DirectImage."""
    if not isinstance(iface, DirectImage):
        raise ModalProxyTypeError(f"Expected DirectImage, got {type(iface).__name__}")
    return iface.image


def _unwrap_app(iface: AppInterface) -> modal.App:
    """Extract the modal.App from a DirectApp."""
    if not isinstance(iface, DirectApp):
        raise ModalProxyTypeError(f"Expected DirectApp, got {type(iface).__name__}")
    return iface.app


def _unwrap_volume(iface: VolumeInterface) -> modal.Volume:
    """Extract the modal.Volume from a DirectVolume."""
    if not isinstance(iface, DirectVolume):
        raise ModalProxyTypeError(f"Expected DirectVolume, got {type(iface).__name__}")
    return iface.volume


def _unwrap_secret(iface: SecretInterface) -> modal.Secret:
    """Extract the modal.Secret from a DirectSecret."""
    if not isinstance(iface, DirectSecret):
        raise ModalProxyTypeError(f"Expected DirectSecret, got {type(iface).__name__}")
    return iface.secret


# ---------------------------------------------------------------------------
# Object implementations
# ---------------------------------------------------------------------------


class DirectExecOutput(ExecOutput):
    """Wraps the stdout stream from a modal sandbox exec result."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    stream: Any = Field(description="The modal stdout stream object", repr=False)

    def read(self) -> str:
        return self.stream.read()


class DirectExecProcess(ExecProcess):
    """Wraps the process object returned by modal sandbox.exec()."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    process: Any = Field(description="The modal ContainerProcess object", repr=False)

    def get_stdout(self) -> ExecOutput:
        return DirectExecOutput.model_construct(stream=self.process.stdout)

    def wait(self) -> None:
        self.process.wait()


class DirectSecret(SecretInterface):
    """Wraps a modal.Secret."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    secret: modal.Secret = Field(description="The underlying modal.Secret", repr=False)


class DirectFunction(FunctionInterface):
    """Wraps a modal.Function."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    function: modal.Function = Field(description="The underlying modal.Function", repr=False)

    def get_web_url(self) -> str | None:
        return self.function.get_web_url()


class DirectImage(ImageInterface):
    """Wraps a modal.Image."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    image: modal.Image = Field(description="The underlying modal.Image", repr=False)

    def get_object_id(self) -> str:
        return self.image.object_id

    def apt_install(self, *packages: str) -> "ImageInterface":
        return DirectImage.model_construct(image=self.image.apt_install(*packages))

    def dockerfile_commands(
        self,
        commands: Sequence[str],
        *,
        context_dir: Path | None = None,
        secrets: Sequence[SecretInterface] = (),
    ) -> "ImageInterface":
        modal_secrets = [_unwrap_secret(s) for s in secrets]
        expanded_context_dir = context_dir.expanduser() if context_dir is not None else None
        new_image = self.image.dockerfile_commands(
            list(commands),
            context_dir=expanded_context_dir,
            secrets=modal_secrets,
        )
        return DirectImage.model_construct(image=new_image)


class DirectVolume(VolumeInterface):
    """Wraps a modal.Volume."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    volume: modal.Volume = Field(description="The underlying modal.Volume", repr=False)
    volume_name: str | None = Field(default=None, description="Volume name if known")

    def get_name(self) -> str | None:
        return self.volume_name

    def listdir(self, path: str) -> list[FileEntry]:
        entries = self.volume.listdir(path)
        return [
            FileEntry(
                path=e.path,
                type=_to_file_entry_type(e.type),
                mtime=e.mtime,
                size=e.size,
            )
            for e in entries
        ]

    def read_file(self, path: str) -> bytes:
        return b"".join(self.volume.read_file(path))

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        self.volume.remove_file(path, recursive=recursive)

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        with self.volume.batch_upload(force=True) as batch:
            for path, file_data in file_contents_by_path.items():
                batch.put_file(io.BytesIO(file_data), path)

    def reload(self) -> None:
        self.volume.reload()

    def commit(self) -> None:
        self.volume.commit()


class DirectSandbox(SandboxInterface):
    """Wraps a modal.Sandbox."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    sandbox: modal.Sandbox = Field(description="The underlying modal.Sandbox", repr=False)

    def get_object_id(self) -> str:
        return self.sandbox.object_id

    def exec(
        self,
        *args: str,
        stdout: StreamType = StreamType.PIPE,
        stderr: StreamType = StreamType.PIPE,
    ) -> ExecProcess:
        process = self.sandbox.exec(
            *args,
            stdout=_to_modal_stream_type(stdout),
            stderr=_to_modal_stream_type(stderr),
        )
        return DirectExecProcess.model_construct(process=process)

    def tunnels(self) -> dict[int, TunnelInfo]:
        raw_tunnels = self.sandbox.tunnels()
        return {port: TunnelInfo(tcp_socket=tunnel.tcp_socket) for port, tunnel in raw_tunnels.items()}

    def get_tags(self) -> dict[str, str]:
        return self.sandbox.get_tags()

    def set_tags(self, tags: Mapping[str, str]) -> None:
        self.sandbox.set_tags(dict(tags))

    def snapshot_filesystem(self, timeout: int = 120) -> ImageInterface:
        image = self.sandbox.snapshot_filesystem(timeout=timeout)
        return DirectImage.model_construct(image=image)

    def terminate(self) -> None:
        self.sandbox.terminate()


class DirectApp(AppInterface):
    """Wraps a modal.App."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    app: modal.App = Field(description="The underlying modal.App", repr=False)

    def get_app_id(self) -> str:
        return self.app.app_id

    def get_name(self) -> str:
        return self.app.name

    @contextlib.contextmanager
    def run(self, *, environment_name: str) -> AbstractContextManager["AppInterface"]:
        with self.app.run(environment_name=environment_name):
            yield self


# ---------------------------------------------------------------------------
# Top-level implementation
# ---------------------------------------------------------------------------


class DirectModalInterface(ModalInterface):
    """Implementation of ModalInterface that calls the real Modal Python SDK."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # =====================================================================
    # Environment
    # =====================================================================

    def environment_create(self, name: str) -> None:
        result = subprocess.run(
            ["modal", "environment", "create", name],
            timeout=30,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ModalProxyError(f"Failed to create Modal environment '{name}': {result.stderr or result.stdout}")

    # =====================================================================
    # App
    # =====================================================================

    def app_create(self, name: str) -> AppInterface:
        return DirectApp.model_construct(app=modal.App(name))

    def app_lookup(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
    ) -> AppInterface:
        app = modal.App.lookup(name, create_if_missing=create_if_missing, environment_name=environment_name)
        return DirectApp.model_construct(app=app)

    # =====================================================================
    # Image
    # =====================================================================

    def image_debian_slim(self) -> ImageInterface:
        return DirectImage.model_construct(image=modal.Image.debian_slim())

    def image_from_registry(self, name: str) -> ImageInterface:
        return DirectImage.model_construct(image=modal.Image.from_registry(name))

    def image_from_id(self, image_id: str) -> ImageInterface:
        return DirectImage.model_construct(image=modal.Image.from_id(image_id))

    # =====================================================================
    # Sandbox
    # =====================================================================

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
    ) -> SandboxInterface:
        modal_volumes: dict[str | os.PathLike[str], modal.Volume | modal.CloudBucketMount] | None = None
        if volumes is not None:
            modal_volumes = {path: _unwrap_volume(vol) for path, vol in volumes.items()}

        sandbox = modal.Sandbox.create(
            image=_unwrap_image(image),
            app=_unwrap_app(app),
            timeout=timeout,
            cpu=cpu,
            memory=memory,
            unencrypted_ports=list(unencrypted_ports),
            gpu=gpu,
            region=region,
            cidr_allowlist=list(cidr_allowlist) if cidr_allowlist is not None else None,
            volumes=modal_volumes,
        )
        return DirectSandbox.model_construct(sandbox=sandbox)

    def sandbox_list(self, *, app_id: str) -> list[SandboxInterface]:
        return [DirectSandbox.model_construct(sandbox=sb) for sb in modal.Sandbox.list(app_id=app_id)]

    def sandbox_from_id(self, sandbox_id: str) -> SandboxInterface:
        return DirectSandbox.model_construct(sandbox=modal.Sandbox.from_id(sandbox_id))

    # =====================================================================
    # Volume
    # =====================================================================

    def volume_from_name(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
        version: int | None = None,
    ) -> VolumeInterface:
        kwargs: dict[str, object] = {
            "create_if_missing": create_if_missing,
            "environment_name": environment_name,
        }
        if version is not None:
            kwargs["version"] = version
        vol = modal.Volume.from_name(name, **kwargs)
        return DirectVolume.model_construct(volume=vol, volume_name=name)

    def volume_list(self, *, environment_name: str) -> list[VolumeInterface]:
        return [
            DirectVolume.model_construct(volume=vol, volume_name=vol.name)
            for vol in modal.Volume.objects.list(environment_name=environment_name)
        ]

    def volume_delete(self, name: str, *, environment_name: str) -> None:
        modal.Volume.objects.delete(name, environment_name=environment_name)

    # =====================================================================
    # Secret
    # =====================================================================

    def secret_from_dict(self, values: Mapping[str, str | None]) -> SecretInterface:
        return DirectSecret.model_construct(secret=modal.Secret.from_dict(dict(values)))

    # =====================================================================
    # Function
    # =====================================================================

    def function_from_name(
        self,
        name: str,
        *,
        app_name: str,
        environment_name: str | None = None,
    ) -> FunctionInterface:
        func = modal.Function.from_name(name=name, app_name=app_name, environment_name=environment_name)
        return DirectFunction.model_construct(function=func)

    # =====================================================================
    # CLI
    # =====================================================================

    def deploy(
        self,
        script_path: Path,
        *,
        app_name: str,
        environment_name: str | None = None,
    ) -> None:
        cmd = ["modal", "deploy"]
        if environment_name is not None:
            cmd.extend(["--env", environment_name])
        cmd.append(str(script_path))

        result = subprocess.run(
            cmd,
            timeout=180,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "MNG_MODAL_APP_NAME": app_name},
        )
        if result.returncode != 0:
            output = (result.stdout + "\n" + result.stderr).strip()
            raise ModalProxyError(f"Failed to deploy {script_path}: {output}")
