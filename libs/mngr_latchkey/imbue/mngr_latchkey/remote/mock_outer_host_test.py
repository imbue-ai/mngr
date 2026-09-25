"""A fake VPS, and a fake latchkey CLI, for exercising the remote modules.

``FakeVps`` is an outer host backed by a directory tree standing in for the
machine: its ``$HOME/.latchkey``, its RAM-backed secrets directory, and the
package's files unpacked where the package would put them. Commands the code
under test sends it run under a real ``sh`` with that tree as the machine, so
what a test exercises is the package's own scripts -- ``mngr-latchkey
read-state`` and ``apply-state`` as built -- rather than a re-enactment of
them. What the scripts shell out to that a test cannot run for real is faked
on PATH: ``latchkey`` (a tiny store-backed CLI, below), ``docker`` (finds the
one container, reports the mappings it was created with, and runs an exec in a
fake container home), ``ssh-keygen``
(mints a stand-in keypair), ``supervisorctl`` (records what it was asked) and
the one ``stat`` probe that says whether the secrets directory is RAM-backed.
The install itself (apt, dpkg) is faked too: the bootstrap command unpacks
whatever ``.deb`` was last uploaded.

The fake latchkey binary reads and writes a plaintext JSON stand-in for
``credentials.json.enc`` recording which accounts a store holds and which key
it was written with, and refuses to open a store under any other key. That is
enough for a credential exchange to be exercised end to end -- what is pulled
really is read back, what is pushed really is what the machine ends up holding
-- without a real latchkey or a real VPS.

``ScriptedOuter`` is the other kind of stand-in: an outer host that runs
nothing and answers by command substring, for code that drives a machine
through many commands and only needs to see which ran, in what order, with
what uploads.
"""

import base64
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.command_logging import is_command_logging_suppressed
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.remote._machine import ANSWER_PREFIX
from imbue.mngr_latchkey.remote._machine import GATEWAY_ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.remote._machine import OUTCOME_DONE_MARKER
from imbue.mngr_latchkey.remote._machine import REMOTE_COMMAND_NAME
from imbue.mngr_latchkey.remote._machine import REMOTE_LATCHKEY_DIR_NAME
from imbue.mngr_latchkey.remote._machine import _ANSWER_HAS_CONTAINER_TUNNEL_KEY
from imbue.mngr_latchkey.remote._machine import _ANSWER_HAS_CREDENTIAL_STORE
from imbue.mngr_latchkey.remote._machine import _ANSWER_HOME
from imbue.mngr_latchkey.remote._machine import _ANSWER_PACKAGE_VERSION
from imbue.mngr_latchkey.remote._machine import _APPLY_STATE_SUBCOMMAND
from imbue.mngr_latchkey.remote._machine import _READ_STATE_SUBCOMMAND
from imbue.mngr_latchkey.remote._mirror import machine_store_dir
from imbue.mngr_latchkey.remote._mirror import materialize_machine_store
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import write_machine_credentials
from imbue.mngr_latchkey.remote.package import CONTAINER_TUNNEL_KEY_FILENAME
from imbue.mngr_latchkey.remote.package import DEFAULT_REMOTE_PACKAGE_LAYOUT
from imbue.mngr_latchkey.remote.package import GATEWAY_CONF_FILENAME
from imbue.mngr_latchkey.remote.package import RemotePackageLayout
from imbue.mngr_latchkey.remote.package import TUNNEL_CONF_FILENAME
from imbue.mngr_latchkey.remote.package import build_remote_package
from imbue.mngr_latchkey.remote.package import remote_package_context
from imbue.mngr_latchkey.store import DESKTOP_EGRESS_RULES_FILENAME
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir
from imbue.mngr_latchkey.testing import extract_deb_data
from imbue.mngr_latchkey.testing import read_deb_control_field

# Stand-in for the key a machine keeps its own credential store under.
MACHINE_KEY = "machine-key-5518"

# The docker bridge address a fake machine resolves by default: where its
# owner-exec daemon and latchkey gateway bind.
DEFAULT_DOCKER_BRIDGE_ADDRESS = "172.17.0.1"

# The ``--add-host`` mapping the VPS provider creates every container with, as
# ``docker inspect`` reports it.
OUTER_HOST_EXTRA_HOST = "host.docker.internal:host-gateway"

# The filesystem type the fake secrets directory reports unless a test says otherwise.
_RAM_BACKED_FSTYPE = "tmpfs"
_SHELL_TIMEOUT_SECONDS = 60.0
_FAKE_BIN_DIR_NAME = "fakebin"


class RecordedCommand(MutableModel):
    """One recorded ``execute_idempotent_command`` invocation."""

    command: str = Field(description="The command string passed to the outer host")
    timeout_seconds: float | None = Field(default=None, description="Timeout passed in (if any)")
    is_kept_out_of_logs: bool = Field(
        default=False, description="Whether the command was issued inside a commands_kept_out_of_logs scope"
    )


class WrittenFile(MutableModel):
    """One recorded ``write_file`` / ``write_text_file`` invocation."""

    path: str = Field(description="Destination path on the VPS")
    content: bytes = Field(description="Bytes written")
    mode: str | None = Field(default=None, description="chmod mode requested (if any)")
    is_atomic: bool = Field(default=True, description="Whether the write was requested atomically (tmp + rename)")


def rooted_layout(root: Path) -> RemotePackageLayout:
    """The default layout with every directory moved under ``root``: the machine as a directory tree."""
    return RemotePackageLayout.model_validate(
        {name: root / path.relative_to("/") for name, path in DEFAULT_REMOTE_PACKAGE_LAYOUT.model_dump().items()}
    )


class FakeVps(MutableModel):
    """An outer host whose machine is a directory tree, running the package's scripts for real.

    Implements only the subset of ``OuterHostInterface`` that the code under
    test touches (``execute_idempotent_command``, the file writes, ``get_name``).
    """

    root: Path = Field(description="The directory standing in for the machine's filesystem root.")
    name: str = Field(default="vps-test", description="Display name returned by get_name")
    container_name: str = Field(
        default="mngr-ws", description="Name of the one container docker finds on the machine."
    )
    container_extra_hosts: tuple[str, ...] = Field(
        default=(OUTER_HOST_EXTRA_HOST,),
        description=(
            "The ``--add-host`` mappings the container was created with, as ``docker inspect`` reports them; "
            "empty for a container created before the outer-host mapping existed."
        ),
    )
    docker_bridge_address: str = Field(
        default=DEFAULT_DOCKER_BRIDGE_ADDRESS,
        description="What the docker-bridge probe resolves to; empty for a machine with no docker bridge.",
    )
    is_local: bool = Field(default=False, description="Whether this outer host is the local machine")
    is_install_failing: bool = Field(default=False, description="Whether the package install is to fail.")
    recorded: list[RecordedCommand] = Field(default_factory=list, description="Each command recorded in order")
    written: list[WrittenFile] = Field(default_factory=list, description="Each file write recorded in order")
    installed_versions: list[str] = Field(
        default_factory=list, description="The version of every package the bootstrap installed, in order."
    )

    @property
    def layout(self) -> RemotePackageLayout:
        return rooted_layout(self.root)

    @property
    def home(self) -> Path:
        return self.root / "root"

    @property
    def latchkey_dir(self) -> Path:
        return self.home / REMOTE_LATCHKEY_DIR_NAME

    @property
    def secrets_dir(self) -> Path:
        return self.layout.secrets_dir

    def get_name(self) -> str:
        return self.name

    # The machine, as a test sets it up.

    def setup(self) -> None:
        """Lay out the machine and install the package on it, the way a provisioned VPS would look."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.parent.mkdir(parents=True, exist_ok=True)
        (self.root / "fstype").write_text(_RAM_BACKED_FSTYPE)
        self._write_fake_binaries()
        self.install_package()

    def install_package(self) -> None:
        """Unpack a build of the package for this machine's layout, as dpkg would."""
        artifact = build_remote_package(remote_package_context(self.layout))
        extract_deb_data(artifact.content, self.root)

    def hold(self, accounts_by_service: Mapping[str, Sequence[str]], key: str = MACHINE_KEY) -> None:
        """Give the machine a credential store holding ``accounts_by_service`` under ``key`` (none when empty)."""
        if not accounts_by_service:
            return
        self.latchkey_dir.mkdir(parents=True, exist_ok=True)
        (self.latchkey_dir / CREDENTIALS_STORE_FILENAME).write_bytes(store_document(accounts_by_service, key))
        (self.latchkey_dir / UPSTREAM_DATA_FORMAT_VERSION_FILENAME).write_text("2")

    def hold_permissions(self, permissions_json: str) -> None:
        self.latchkey_dir.mkdir(parents=True, exist_ok=True)
        (self.latchkey_dir / PERMISSIONS_CONFIG_FILENAME).write_text(permissions_json)

    def hold_desktop_egress_rules(self, desktop_egress_rules_json: str) -> None:
        self.latchkey_dir.mkdir(parents=True, exist_ok=True)
        (self.latchkey_dir / DESKTOP_EGRESS_RULES_FILENAME).write_text(desktop_egress_rules_json)

    def hold_config(self, config_json: str) -> None:
        self.latchkey_dir.mkdir(parents=True, exist_ok=True)
        (self.latchkey_dir / CONFIG_FILENAME).write_text(config_json)

    def run_under(self, secret_filename: str, value: str) -> None:
        """Put a secret in the machine's RAM-backed directory, as its running gateway would have it."""
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        (self.secrets_dir / secret_filename).write_text(value)

    def run_under_key(self, key: str) -> None:
        self.run_under(GATEWAY_ENCRYPTION_KEY_FILENAME, key)

    def set_secrets_dir_filesystem_type(self, filesystem_type: str) -> None:
        (self.root / "fstype").write_text(filesystem_type)

    def fail_supervisorctl_for(self, program_name: str) -> None:
        """Make every supervisorctl call naming ``program_name`` fail, as when the program does not come up."""
        (self.root / "supervisorctl.fail").write_text(program_name)

    # The machine, as a test reads it back.

    def secret(self, filename: str) -> str | None:
        path = self.secrets_dir / filename
        return path.read_text().strip() if path.is_file() else None

    def secret_mode(self, filename: str) -> int:
        return stat.S_IMODE((self.secrets_dir / filename).stat().st_mode)

    def machine_accounts(self) -> dict[str, list[str]]:
        path = self.latchkey_dir / CREDENTIALS_STORE_FILENAME
        return store_accounts(path.read_bytes()) if path.is_file() else {}

    def machine_permissions(self) -> str | None:
        path = self.latchkey_dir / PERMISSIONS_CONFIG_FILENAME
        return path.read_text() if path.is_file() else None

    def machine_desktop_egress_rules(self) -> str | None:
        path = self.latchkey_dir / DESKTOP_EGRESS_RULES_FILENAME
        return path.read_text() if path.is_file() else None

    def machine_config(self) -> str | None:
        path = self.latchkey_dir / CONFIG_FILENAME
        return path.read_text() if path.is_file() else None

    def tunnel_conf(self) -> str | None:
        path = self.latchkey_dir / TUNNEL_CONF_FILENAME
        return path.read_text() if path.is_file() else None

    def gateway_conf(self) -> str | None:
        path = self.latchkey_dir / GATEWAY_CONF_FILENAME
        return path.read_text() if path.is_file() else None

    def tunnel_key(self) -> str | None:
        path = self.latchkey_dir / CONTAINER_TUNNEL_KEY_FILENAME
        return path.read_text() if path.is_file() else None

    def predate_outer_host_mapping(self) -> None:
        """Make the container one created before containers carried the outer-host mapping."""
        self.container_extra_hosts = ()

    def mint_tunnel_key(self) -> None:
        """Leave the keypair an earlier build's tunnel authenticated with, as a machine it tunneled into holds."""
        self.latchkey_dir.mkdir(parents=True, exist_ok=True)
        (self.latchkey_dir / CONTAINER_TUNNEL_KEY_FILENAME).write_text("FAKE PRIVATE KEY earlier\n")
        (self.latchkey_dir / f"{CONTAINER_TUNNEL_KEY_FILENAME}.pub").write_text("ssh-ed25519 FAKEearlier fake@vps\n")

    def latchkey_dir_entries(self) -> list[str]:
        return sorted(path.name for path in self.latchkey_dir.iterdir()) if self.latchkey_dir.is_dir() else []

    def secrets_dir_entries(self) -> list[str]:
        return sorted(path.name for path in self.secrets_dir.iterdir()) if self.secrets_dir.is_dir() else []

    def supervisorctl_calls(self) -> list[str]:
        path = self.root / "supervisorctl.log"
        return path.read_text().splitlines() if path.is_file() else []

    def latchkey_calls(self) -> list[str]:
        path = self.root / "latchkey.log"
        return path.read_text().splitlines() if path.is_file() else []

    def docker_calls(self) -> list[str]:
        path = self.root / "docker.log"
        return path.read_text().splitlines() if path.is_file() else []

    def container_authorized_keys(self, container: str, user: str) -> str:
        path = self.root / "containers" / container / user / ".ssh" / "authorized_keys"
        return path.read_text() if path.is_file() else ""

    def recorded_commands(self) -> list[str]:
        return [entry.command for entry in self.recorded]

    def has_received(self, secret: str) -> bool:
        """Whether ``secret`` reached the machine: in a command, in a document entry (decoded), or in a file."""
        encoded_secret = secret.encode("utf-8")
        return (
            any(secret in command for command in self.recorded_commands())
            or any(encoded_secret in value for document in self._sent_documents() for value in document.values())
            or any(encoded_secret in entry.content for entry in self.written)
        )

    def _sent_documents(self) -> list[dict[str, bytes]]:
        """Every document a machine command carried, entry by entry and decoded."""
        documents: list[dict[str, bytes]] = []
        for command in self.recorded_commands():
            first_line, *document_lines = command.splitlines()
            if first_line.startswith(REMOTE_COMMAND_NAME):
                entries = (line.split(" ", 1) for line in document_lines[:-1])
                documents.append({name: base64.b64decode(value) for name, value in entries})
        return documents

    # OuterHostInterface.

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(
            RecordedCommand(
                command=command, timeout_seconds=timeout_seconds, is_kept_out_of_logs=is_command_logging_suppressed()
            )
        )
        # The owner-exec vm daemon's install and start (which curl a release and
        # drive systemd) and its docker-bridge probe are answered rather than
        # run; the package bootstrap (apt, dpkg) is acted out by unpacking the
        # uploaded package; everything else runs on the fake machine.
        if "owner-exec" in command:
            return CommandResult(stdout="", stderr="", success=True)
        if "addr show docker0" in command:
            return CommandResult(stdout=f"{self.docker_bridge_address}\n", stderr="", success=True)
        if "dpkg -i" in command:
            return self._install_uploaded_package()
        return self._run_shell(command)

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = True) -> None:
        self.written.append(WrittenFile(path=str(path), content=content, mode=mode, is_atomic=is_atomic))
        local_path = self._local_path(path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        if mode is not None:
            local_path.chmod(int(mode, 8))

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
        is_atomic: bool = True,
    ) -> None:
        self.write_file(path, content.encode(encoding), mode=mode, is_atomic=is_atomic)

    def _local_path(self, path: Path) -> Path:
        """Where a path on the machine lives here: a layout path is already under the root, any other is rooted."""
        return path if path.is_relative_to(self.root) else self.root / path.relative_to("/")

    def _run_shell(self, command: str) -> CommandResult:
        (self.root / "docker-container-name").write_text(self.container_name)
        # ``docker inspect`` renders a container created with no mapping as ``null``.
        (self.root / "docker-container-extra-hosts").write_text(
            json.dumps(list(self.container_extra_hosts)) if self.container_extra_hosts else "null"
        )
        completed = subprocess.run(
            ["sh", "-c", command],
            env=self._shell_environment(),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=_SHELL_TIMEOUT_SECONDS,
        )
        return CommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            success=completed.returncode == 0,
            exit_code=completed.returncode,
        )

    def _shell_environment(self) -> dict[str, str]:
        return {
            "PATH": os.pathsep.join(
                (str(self.layout.bin_dir), str(self.root / _FAKE_BIN_DIR_NAME), os.environ["PATH"])
            ),
            "HOME": str(self.home),
            "FAKE_VPS_ROOT": str(self.root),
        }

    def _install_uploaded_package(self) -> CommandResult:
        if self.is_install_failing:
            return CommandResult(stdout="", stderr="E: Unable to locate package nodejs", success=False)
        uploads = [entry for entry in self.written if entry.path.endswith(".deb")]
        assert uploads, "the bootstrap ran before any package was uploaded"
        content = uploads[-1].content
        extract_deb_data(content, self.root)
        version = read_deb_control_field(content, "Version")
        self.installed_versions.append(version)
        return CommandResult(stdout=f"Unpacking mngr-latchkey ({version}) ...\n", stderr="", success=True)

    def _write_fake_binaries(self) -> None:
        bin_dir = self.root / _FAKE_BIN_DIR_NAME
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "latchkey").symlink_to(fake_latchkey_binary(bin_dir))
        real_stat = shutil.which("stat")
        assert real_stat is not None
        for name, source in (
            ("docker", _FAKE_DOCKER_SOURCE),
            ("ssh-keygen", _FAKE_SSH_KEYGEN_SOURCE),
            ("supervisorctl", _FAKE_SUPERVISORCTL_SOURCE),
            ("stat", _FAKE_STAT_SOURCE.replace("REAL_STAT", real_stat)),
        ):
            script = bin_dir / name
            script.write_text(source)
            script.chmod(0o755)


def fake_vps(
    tmp_path: Path,
    accounts_by_service: Mapping[str, Sequence[str]] | None = None,
    machine_permissions: str | None = None,
    container_name: str = "mngr-ws",
    is_local: bool = False,
) -> OuterHostInterface:
    """A fake VPS with the package installed, holding a credential store (and optionally a policy) of its own.

    Its RAM-backed secrets directory starts empty, like a machine that rebooted
    since it was provisioned; ``run_under_key`` gives it the key its gateway
    would be running under. ``cast`` is used because the fake is
    structurally-but-not-nominally an OuterHostInterface (the interface has many
    other abstract methods that the code under test never calls).
    """
    vps = FakeVps(root=tmp_path / "vps", container_name=container_name, is_local=is_local)
    vps.setup()
    vps.hold(accounts_by_service or {})
    if machine_permissions is not None:
        vps.hold_permissions(machine_permissions)
    return cast(OuterHostInterface, vps)


def as_vps(outer: OuterHostInterface) -> FakeVps:
    return cast(FakeVps, outer)


class AnsweringVps(FakeVps):
    """A fake machine that answers every command with a canned result instead of running it."""

    canned: CommandResult = Field(description="What every command is answered with.")

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(
            RecordedCommand(
                command=command, timeout_seconds=timeout_seconds, is_kept_out_of_logs=is_command_logging_suppressed()
            )
        )
        return self.canned


class ScriptedOuter(MutableModel):
    """An outer host that runs nothing: it answers by command substring and logs commands and writes in order.

    A ``read-state`` is answered as a machine holding nothing whose home is
    ``home``, an ``apply-state`` as applied, and any other command with an
    empty success -- unless ``result_by_substring`` names it (first match
    wins). Implements only the subset of ``OuterHostInterface`` the code under
    test touches.
    """

    name: str = Field(default="vps-test", description="Display name returned by get_name")
    home: Path = Field(default=Path("/root"), description="The home a read-state answers with.")
    docker_bridge_address: str = Field(
        default=DEFAULT_DOCKER_BRIDGE_ADDRESS, description="What the docker-bridge probe is answered with."
    )
    is_local: bool = Field(default=False, description="Whether this outer host is the local machine")
    result_by_substring: dict[str, CommandResult] = Field(
        default_factory=dict, description="The result for any command containing the key (first match wins)"
    )
    events: list[str] = Field(default_factory=list, description="``run:<command>`` and ``write:<path>`` in order")
    recorded: list[RecordedCommand] = Field(default_factory=list, description="Each command recorded in order")
    written: list[WrittenFile] = Field(default_factory=list, description="Each file write recorded in order")

    def get_name(self) -> str:
        return self.name

    def recorded_commands(self) -> list[str]:
        return [entry.command for entry in self.recorded]

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(
            RecordedCommand(
                command=command, timeout_seconds=timeout_seconds, is_kept_out_of_logs=is_command_logging_suppressed()
            )
        )
        self.events.append(f"run:{command}")
        for substring, result in self.result_by_substring.items():
            if substring in command:
                return result
        if "addr show docker0" in command:
            return CommandResult(stdout=f"{self.docker_bridge_address}\n", stderr="", success=True)
        canned = canned_machine_answer(command, self.home)
        return canned if canned is not None else CommandResult(stdout="", stderr="", success=True)

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = True) -> None:
        self.written.append(WrittenFile(path=str(path), content=content, mode=mode, is_atomic=is_atomic))
        self.events.append(f"write:{path}")

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
        is_atomic: bool = True,
    ) -> None:
        self.write_file(path, content.encode(encoding), mode=mode, is_atomic=is_atomic)


def scripted_outer(**kwargs: object) -> tuple[OuterHostInterface, ScriptedOuter]:
    """A ``ScriptedOuter`` both as the interface the code under test takes and as itself, for the assertions."""
    stub = ScriptedOuter.model_validate(kwargs)
    return cast(OuterHostInterface, stub), stub


def canned_machine_answer(command: str, home: Path) -> CommandResult | None:
    """What a machine holding nothing answers one of the package's two commands with; ``None`` for any other command."""
    first_line = command.split("\n", 1)[0]
    if first_line.startswith(f"{REMOTE_COMMAND_NAME} {_READ_STATE_SUBCOMMAND}"):
        answers = {
            _ANSWER_PACKAGE_VERSION: b"0+test",
            _ANSWER_HOME: str(home).encode("utf-8"),
            _ANSWER_HAS_CREDENTIAL_STORE: b"0",
            _ANSWER_HAS_CONTAINER_TUNNEL_KEY: b"0",
        }
        lines = [f"{ANSWER_PREFIX}{name}={base64.b64encode(value).decode('ascii')}" for name, value in answers.items()]
        return CommandResult(stdout="\n".join((*lines, OUTCOME_DONE_MARKER)) + "\n", stderr="", success=True)
    if first_line.startswith(f"{REMOTE_COMMAND_NAME} {_APPLY_STATE_SUBCOMMAND}"):
        return CommandResult(stdout=f"{OUTCOME_DONE_MARKER}\n", stderr="", success=True)
    return None


def store_document(accounts_by_service: Mapping[str, Sequence[str]], key: str = "") -> bytes:
    return json.dumps(
        {"accounts": {name: list(accounts) for name, accounts in accounts_by_service.items()}, "key": key}
    ).encode("utf-8")


def store_accounts(content: bytes) -> dict[str, list[str]]:
    return dict(json.loads(content.decode("utf-8"))["accounts"])


# Built from quoted lines rather than one triple-quoted block because a fake
# CLI's whole job is to write to stdout, and a block would put those writes at
# the start of a physical line -- where they read as this file printing rather
# than the program it carries.
_FAKE_LATCHKEY_SOURCE = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "STORE = 'credentials.json.enc'\n"
    "def load(directory):\n"
    "    try:\n"
    "        with open(os.path.join(directory, STORE)) as handle:\n"
    "            return json.load(handle)\n"
    "    except FileNotFoundError:\n"
    "        return {'accounts': {}, 'key': ''}\n"
    "def accounts_of(data, name):\n"
    "    return {account: {'credentialType': 'oauth', 'credentialStatus': 'valid'}\n"
    "            for account in data['accounts'].get(name, [])}\n"
    # A store written under a key opens only under that key, as the real
    # store does, and is refused in upstream's words, which is what a caller
    # tells a wrong key from any other failure by; one written under none (the
    # desktop's, in these tests) opens under any.
    "WRONG_KEY = ('Error: Failed to read credential store: Failed to decrypt file: Failed to decrypt data: '\n"
    "             'Unsupported state or unable to authenticate data. The encryption key may have changed.')\n"
    "def require_key(data):\n"
    "    if data['key'] and data['key'] != os.environ.get('LATCHKEY_ENCRYPTION_KEY', ''):\n"
    "        sys.exit(WRONG_KEY)\n"
    "directory = os.environ['LATCHKEY_DIRECTORY']\n"
    "argv = sys.argv[1:]\n"
    # On the fake machine every invocation is logged, so a test can see what
    # the scripts told the CLI to do.
    "if 'FAKE_VPS_ROOT' in os.environ:\n"
    "    with open(os.path.join(os.environ['FAKE_VPS_ROOT'], 'latchkey.log'), 'a') as log:\n"
    "        log.write(' '.join(argv) + '\\n')\n"
    "data = load(directory)\n"
    "if argv[:2] == ['auth', 'list']:\n"
    "    require_key(data)\n"
    "    out = {name: accounts_of(data, name) for name in data['accounts']}\n"
    "elif argv[:2] == ['services', 'info']:\n"
    "    require_key(data)\n"
    "    out = {'credentials': accounts_of(data, argv[2])}\n"
    "elif argv[:2] == ['auth', 'clear']:\n"
    "    require_key(data)\n"
    "    rest = [item for item in argv[2:] if item != '-y']\n"
    "    service, account = rest[0], rest[rest.index('--account') + 1]\n"
    "    remaining = [entry for entry in data['accounts'].get(service, []) if entry != account]\n"
    "    if remaining:\n"
    "        data['accounts'][service] = remaining\n"
    "    else:\n"
    "        data['accounts'].pop(service, None)\n"
    "    with open(os.path.join(directory, STORE), 'w') as handle:\n"
    "        json.dump(data, handle)\n"
    "    out = None\n"
    "elif argv[:2] == ['auth', 're-encrypt']:\n"
    "    require_key(data)\n"
    # Upstream's wording, repeated rather than imported: the script under test
    # copes with what the real CLI prints, so the fake has to print that and
    # not whatever the script happens to look for.
    "    if not data['accounts'] and '--services' not in argv:\n"
    "        sys.exit('Error: No stored credentials found to re-encrypt.')\n"
    "    destination, rest = argv[2], argv[3:]\n"
    "    account = None\n"
    "    if '--account' in rest:\n"
    "        account = rest[rest.index('--account') + 1]\n"
    "        rest = rest[: rest.index('--account')]\n"
    "    wanted = rest[1:] if rest[:1] == ['--services'] else None\n"
    # The real CLI decrypts the destination to merge into it, so a store there
    # written under a key other than the one it is to be written under (stdin,
    # else the source's) is refused the way any store under the wrong key is.
    "    destination_key = sys.stdin.read().strip() or data['key']\n"
    "    existing = load(destination)\n"
    "    if existing['key'] and existing['key'] != destination_key:\n"
    "        sys.exit(WRONG_KEY)\n"
    "    merged = dict(existing['accounts'])\n"
    "    for name, accounts in data['accounts'].items():\n"
    "        if wanted is not None and name not in wanted:\n"
    "            continue\n"
    "        if account is None:\n"
    "            merged[name] = sorted(accounts)\n"
    "        elif account in accounts:\n"
    "            merged[name] = sorted(set(merged.get(name, [])) | {account})\n"
    "    os.makedirs(destination, exist_ok=True)\n"
    "    with open(os.path.join(destination, STORE), 'w') as handle:\n"
    "        json.dump({'accounts': merged, 'key': destination_key}, handle)\n"
    # The real CLI runs its migrations against the destination and stamps it.
    "    with open(os.path.join(destination, 'data-format-version'), 'w') as handle:\n"
    "        handle.write('2')\n"
    "    out = None\n"
    "else:\n"
    "    sys.exit('unsupported invocation: ' + ' '.join(argv))\n"
    "if out is not None:\n"
    "    sys.stdout.write(json.dumps(out))\n"
)

# ``docker ps`` finds the one container the fake machine runs (none when the
# name is empty); ``docker inspect`` reports the mappings it was created with
# the way the real one formats ``{{json .HostConfig.ExtraHosts}}``; ``docker
# exec`` runs the command in a home directory of its own per container and
# user, with the ``-e`` variables in its environment, so an authorized_keys
# append really lands somewhere a test can read.
_FAKE_DOCKER_SOURCE = (
    "#!/usr/bin/env python3\n"
    "import os, pathlib, subprocess, sys\n"
    "root = pathlib.Path(os.environ['FAKE_VPS_ROOT'])\n"
    "argv = sys.argv[1:]\n"
    "with (root / 'docker.log').open('a') as log:\n"
    "    log.write(' '.join(argv) + '\\n')\n"
    "if argv[:1] == ['ps']:\n"
    "    name = (root / 'docker-container-name').read_text().strip()\n"
    "    if name:\n"
    "        print(name)\n"
    "elif argv[:1] == ['inspect']:\n"
    "    name = (root / 'docker-container-name').read_text().strip()\n"
    "    if argv[1:] != ['-f', '{{json .HostConfig.ExtraHosts}}', name]:\n"
    "        sys.exit('Error: No such object: ' + ' '.join(argv[1:]))\n"
    "    print((root / 'docker-container-extra-hosts').read_text().strip())\n"
    "elif argv[:1] == ['exec']:\n"
    "    rest, user, extra = argv[1:], 'root', {}\n"
    "    while rest[0].startswith('-'):\n"
    "        flag = rest.pop(0)\n"
    "        if flag == '-u':\n"
    "            user = rest.pop(0)\n"
    "        elif flag == '-e':\n"
    "            key, _, value = rest.pop(0).partition('=')\n"
    "            extra[key] = value\n"
    "        else:\n"
    "            sys.exit('unsupported docker exec flag: ' + flag)\n"
    "    container = rest.pop(0)\n"
    "    home = root / 'containers' / container / user\n"
    "    home.mkdir(parents=True, exist_ok=True)\n"
    "    sys.exit(subprocess.call(rest, env={**os.environ, **extra, 'HOME': str(home)}))\n"
    "else:\n"
    "    sys.exit('unsupported docker invocation: ' + ' '.join(argv))\n"
)

# Mints a "keypair" at the ``-f`` path: a private file and a ``.pub`` beside it,
# unique per mint so a test can tell a re-minted key from a reused one.
_FAKE_SSH_KEYGEN_SOURCE = (
    "#!/bin/sh\n"
    "set -eu\n"
    "while [ $# -gt 1 ]; do\n"
    '  if [ "$1" = -f ]; then _key="$2"; fi\n'
    "  shift\n"
    "done\n"
    "_nonce=\"$(od -An -N8 -tx1 /dev/urandom | tr -d ' \\n')\"\n"
    'printf \'FAKE PRIVATE KEY %s\\n\' "$_nonce" > "$_key"\n'
    'chmod 600 "$_key"\n'
    'printf \'ssh-ed25519 FAKE%s fake@vps\\n\' "$_nonce" > "$_key.pub"\n'
)

# Records every call; a call naming the program a test marked as failing
# (``fail_supervisorctl_for``) exits non-zero the way the real one does when
# the program does not come up.
_FAKE_SUPERVISORCTL_SOURCE = (
    "#!/bin/sh\n"
    'printf \'%s\\n\' "$*" >> "$FAKE_VPS_ROOT/supervisorctl.log"\n'
    'if [ -f "$FAKE_VPS_ROOT/supervisorctl.fail" ]; then\n'
    '  case " $* " in *" $(cat "$FAKE_VPS_ROOT/supervisorctl.fail") "*) exit 1 ;; esac\n'
    "fi\n"
)

# Only the one probe the package's scripts make is faked: the filesystem type
# of a directory, answered from a file a test can change.
_FAKE_STAT_SOURCE = (
    "#!/bin/sh\n"
    'if [ "$1" = -f ] && [ "$2" = -c ] && [ "$3" = %T ]; then\n'
    '  cat "$FAKE_VPS_ROOT/fstype"\n'
    "  exit 0\n"
    "fi\n"
    'exec REAL_STAT "$@"\n'
)


def fake_latchkey_binary(tmp_path: Path) -> Path:
    script = tmp_path / "fake-latchkey"
    script.write_text(_FAKE_LATCHKEY_SOURCE)
    script.chmod(0o755)
    return script


def desktop_latchkey(
    tmp_path: Path,
    *,
    host_id: HostId,
    desktop_accounts: Mapping[str, Sequence[str]] | None = None,
    machine_accounts: Mapping[str, Sequence[str]] | None = None,
) -> Latchkey:
    """A desktop whose machine store for ``host_id`` holds ``machine_accounts``."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    (latchkey_directory / UPSTREAM_DATA_FORMAT_VERSION_FILENAME).write_text("2")
    (latchkey_directory / CREDENTIALS_STORE_FILENAME).write_bytes(store_document(desktop_accounts or {}))
    latchkey = Latchkey(latchkey_directory=latchkey_directory, latchkey_binary=str(fake_latchkey_binary(tmp_path)))
    data_dir = plugin_data_dir(latchkey_directory)
    store_machine_encryption_key(data_dir, host_id, SecretStr(MACHINE_KEY))
    materialize_machine_store(latchkey_directory, data_dir, host_id)
    if machine_accounts is not None:
        write_machine_credentials(machine_store_dir(data_dir, host_id), store_document(machine_accounts), "2")
    return latchkey


def grant_host_permissions(latchkey: Latchkey, host_id: HostId, rules_json: str) -> None:
    permissions_path = permissions_path_for_host(plugin_data_dir(latchkey.latchkey_directory), host_id)
    permissions_path.parent.mkdir(parents=True, exist_ok=True)
    permissions_path.write_text(rules_json)


SLACK_GRANTED = '{"rules": [{"slack-api": ["slack-read-all"]}]}'
