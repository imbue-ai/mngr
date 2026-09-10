"""A stub VPS outer host, and a fake latchkey CLI, for exercising the remote modules.

``StubOuter`` records every command and file write it is handed and acts out
the handful of remote behaviors the code under test relies on ($HOME
resolution, the docker container lookup, the credential handover's ``latchkey``
invocations). The fake latchkey binary is a tiny store-backed CLI: it reads and
writes a plaintext JSON stand-in for ``credentials.json.enc`` recording which
accounts a store holds and which key it was written with. That is enough for a
credential exchange to be exercised end to end -- what is pulled really is read
back, what is pushed really is what the machine ends up holding -- without a
real latchkey or a real VPS.
"""

import base64
import hashlib
import json
import re
import shlex
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
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.remote._machine import REMOTE_LATCHKEY_DIR_NAME
from imbue.mngr_latchkey.remote._mirror import machine_store_dir
from imbue.mngr_latchkey.remote._mirror import materialize_machine_store
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import write_machine_credentials
from imbue.mngr_latchkey.remote._transfer import _MachineScriptOutcome
from imbue.mngr_latchkey.remote._transfer import _READ_CREDENTIALS_PREFIX
from imbue.mngr_latchkey.remote._transfer import _READ_DATA_FORMAT_VERSION_PREFIX
from imbue.mngr_latchkey.remote._transfer import _READ_PERMISSIONS_PREFIX
from imbue.mngr_latchkey.remote._transfer import _SCRIPT_OUTCOME_PREFIX
from imbue.mngr_latchkey.remote._transfer import _outcome_marker
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

# Stand-in for the key a machine keeps its own credential store under.
MACHINE_KEY = "machine-key-5518"

# The variable block a single-round-trip machine script opens with: every
# payload it carries is assigned to a ``_lk_*`` shell variable before the body.
_SCRIPT_VARIABLE_LINE = re.compile(r"^(_lk_[a-z0-9_]+)=(.*)$")


class RecordedCommand(MutableModel):
    """One recorded ``execute_idempotent_command`` invocation."""

    command: str = Field(description="The command string passed to the outer host")
    timeout_seconds: float | None = Field(default=None, description="Timeout passed in (if any)")


class WrittenFile(MutableModel):
    """One recorded ``write_file`` / ``write_text_file`` invocation."""

    path: str = Field(description="Destination path on the VPS")
    content: bytes = Field(description="Bytes written")
    mode: str | None = Field(default=None, description="chmod mode requested (if any)")
    is_atomic: bool = Field(default=False, description="Whether the write was requested atomically (tmp + rename)")


class StubOuter(MutableModel):
    """Stub outer host that records commands / writes and returns a canned result.

    Implements only the subset of ``OuterHostInterface`` that the functions
    under test touch (``execute_idempotent_command``, ``write_file``,
    ``write_text_file``, ``get_name``).
    """

    name: str = Field(default="vps-test", description="Display name returned by get_name")
    result: CommandResult = Field(
        default_factory=lambda: CommandResult(stdout="", stderr="", success=True),
        description="Canned result returned for every command",
    )
    home: str = Field(default="/root", description="Value returned for the $HOME resolution command")
    config_json: str | None = Field(
        default=None, description="Pre-existing ~/.latchkey/config.json content on the VPS (None means absent)"
    )
    container_name: str = Field(default="mngr-ws", description="Container name returned for the 'docker ps' lookup")
    is_remote_latchkey_dir_present: bool = Field(
        default=False,
        description="Whether ~/.latchkey already exists on the VPS (i.e. an older build provisioned it)",
    )
    machine_accounts: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Accounts this machine's own credential store holds, keyed by service.",
    )
    machine_store_key: str = Field(
        default=MACHINE_KEY,
        description="The key this machine's own credential store is encrypted under.",
    )
    machine_permissions: str | None = Field(
        default=None, description="The permissions policy this machine is enforcing, or None when it has none."
    )
    remote_files: dict[str, bytes] = Field(
        default_factory=dict, description="Files written to the machine, by absolute path."
    )
    latchkey_commands: list[str] = Field(
        default_factory=list, description="Every ``latchkey`` invocation this machine was asked to run."
    )
    is_local: bool = Field(default=False, description="Whether this outer host is the local machine")
    recorded: list[RecordedCommand] = Field(default_factory=list, description="Each command recorded in order")
    written: list[WrittenFile] = Field(default_factory=list, description="Each file write recorded in order")

    def get_name(self) -> str:
        return self.name

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(RecordedCommand(command=command, timeout_seconds=timeout_seconds))
        # Only the dedicated $HOME-resolution probe gets the home response; the
        # container lookup returns the configured name; the owner-exec vm
        # docker-bridge probe resolves to a bridge address (so the vm daemon
        # provisioning that provision_remote_gateway now runs succeeds instead of
        # failing closed); everything else (install/gateway/keypair/tunnel
        # scripts) returns the configured result.
        if command.strip() == 'echo "$HOME"':
            return CommandResult(stdout=f"{self.home}\n", stderr="", success=True)
        if command.startswith("docker ps"):
            return CommandResult(stdout=f"{self.container_name}\n", stderr="", success=True)
        if "addr show docker0" in command:
            return CommandResult(stdout="172.17.0.1\n", stderr="", success=True)
        if _SCRIPT_OUTCOME_PREFIX in command:
            return self._run_machine_script(command)
        if "latchkey auth" in command:
            return self._run_machine_latchkey(command)
        if command.startswith("rm -f ") and CREDENTIALS_STORE_FILENAME in command:
            # Abandoning the machine's own store, not a scratch path.
            self.machine_accounts.clear()
            return CommandResult(stdout="", stderr="", success=True)
        if command.startswith("rm -rf"):
            removed = shlex.split(command)[-1]
            for path in [path for path in self.remote_files if path.startswith(removed)]:
                del self.remote_files[path]
            self.remote_files.pop(removed, None)
            return CommandResult(stdout="", stderr="", success=True)
        return self.result

    def path_exists(self, path: Path) -> bool:
        # Files the test (or the code under test) actually wrote win over the
        # canned answers below -- notably the gateway's tmpfs key file.
        if str(path) in self.remote_files:
            return True
        if path.name == REMOTE_LATCHKEY_DIR_NAME:
            return self.is_remote_latchkey_dir_present
        if str(path) == "/root/.latchkey/credentials.json.enc":
            return bool(self.machine_accounts)
        if path.name == PERMISSIONS_CONFIG_FILENAME:
            return self.machine_permissions is not None
        return path.name == CONFIG_FILENAME and self.config_json is not None

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        written = self.remote_files.get(str(path))
        if written is not None:
            return written.decode(encoding)
        if path.name == CONFIG_FILENAME and self.config_json is not None:
            return self.config_json
        if path.name == UPSTREAM_DATA_FORMAT_VERSION_FILENAME:
            return "2"
        if path.name == PERMISSIONS_CONFIG_FILENAME and self.machine_permissions is not None:
            return self.machine_permissions
        raise FileNotFoundError(str(path))

    def read_file(self, path: Path) -> bytes:
        content = self.remote_files.get(str(path))
        if content is None:
            raise FileNotFoundError(str(path))
        return content

    def recorded_commands(self) -> list[str]:
        return [entry.command for entry in self.recorded]

    def _run_machine_latchkey(self, script: str) -> CommandResult:
        """Act out the one bare ``latchkey`` invocation left: provisioning's key-verification probe.

        Everything else -- every push, and the read-back -- travels as a machine
        script instead (:meth:`_run_machine_script`). Anything else here is a
        test writing a command this machine was never taught, which fails
        loudly rather than passing vacuously.
        """
        self.latchkey_commands.append(script)
        assert "auth list" in script, f"this machine was never taught to run: {script}"
        # Succeeds only under the key this machine's store is actually written with.
        is_readable = _script_encryption_key(script) == self.machine_store_key
        return CommandResult(
            stdout="",
            stderr="" if is_readable else "Error: Failed to decrypt the credential store.",
            success=is_readable,
        )

    def _merge_uploaded_bundle(self, uploaded: bytes, selected: Sequence[str], account: str | None) -> None:
        """Take the named services (and, when named, the one account) of a bundle into this machine's store.

        What the bundle carries beyond them is not this machine's business.
        Mirrors the real CLI: without ``--account`` a selected service is
        *replaced* by the bundle's copy; with it, only that account is
        overwritten and the service's other accounts stay exactly as the
        machine holds them.
        """
        for service_name, accounts in store_accounts(uploaded).items():
            if service_name not in selected:
                continue
            if account is None:
                self.machine_accounts[service_name] = sorted(accounts)
            elif account in accounts:
                merged = set(self.machine_accounts.get(service_name, [])) | {account}
                self.machine_accounts[service_name] = sorted(merged)
            else:
                # The bundle carries nothing for this account of this service,
                # so there is nothing to take from it -- as with the real CLI.
                pass

    def _run_machine_script(self, script: str) -> CommandResult:
        """Act out a single-round-trip machine script, from the variables it opens with.

        Mirrors what the script does on a real machine, in order: report a
        missing tmpfs key (a script that touches the credential store needs
        one), refuse a key that is not the one the script expects (only a
        script that *brings* credential material names one), then apply
        whichever halves it carries, credential first and policy second.
        """
        self.latchkey_commands.append(script)
        variable_by_name = _parse_script_variables(script)
        if "_lk_out_key" in variable_by_name:
            return self._answer_machine_read(variable_by_name)
        key_file = variable_by_name.get("_lk_key_file")
        expected_key_sha256 = variable_by_name.get("_lk_expected_key_sha256")
        if key_file is not None:
            key_content = self.remote_files.get(key_file)
            if key_content is None or not key_content.strip():
                return CommandResult(
                    stdout=f"{_outcome_marker(_MachineScriptOutcome.KEY_MISSING)}\n", stderr="", success=True
                )
            if (
                expected_key_sha256 is not None
                and hashlib.sha256(key_content.strip()).hexdigest() != expected_key_sha256
            ):
                return CommandResult(
                    stdout="", stderr="Error: the machine runs under a different key\n", success=False
                )
        config_b64 = variable_by_name.get("_lk_config_b64")
        if config_b64 is not None:
            config = base64.b64decode(config_b64)
            remote_dir = variable_by_name["_lk_remote_dir"].replace("$HOME", self.home)
            self.remote_files[f"{remote_dir}/{CONFIG_FILENAME}"] = config
            self.config_json = config.decode("utf-8")
        bundle_b64 = variable_by_name.get("_lk_bundle_b64")
        if bundle_b64 is not None:
            self._merge_uploaded_bundle(
                base64.b64decode(bundle_b64),
                [variable_by_name["_lk_service"]],
                variable_by_name["_lk_account"] or None,
            )
        if "latchkey auth clear" in script:
            self._clear_account(variable_by_name["_lk_service"], variable_by_name["_lk_account"])
        permissions_b64 = variable_by_name.get("_lk_permissions_b64")
        if permissions_b64 is not None:
            permissions = base64.b64decode(permissions_b64)
            remote_dir = variable_by_name["_lk_remote_dir"].replace("$HOME", self.home)
            self.remote_files[f"{remote_dir}/{PERMISSIONS_CONFIG_FILENAME}"] = permissions
            self.machine_permissions = permissions.decode("utf-8")
        return CommandResult(stdout=f"{_outcome_marker(_MachineScriptOutcome.APPLIED)}\n", stderr="", success=True)

    def _answer_machine_read(self, variable_by_name: Mapping[str, str]) -> CommandResult:
        """Act out the single-round-trip read of everything this machine holds.

        The policy is answered whatever key the machine is running under -- it
        is not encrypted -- while the credential store needs the tmpfs key, so a
        machine that has one but lost the key reports that instead, exactly as
        the script does.
        """
        lines: list[str] = []
        if self.machine_permissions is not None:
            lines.append(_READ_PERMISSIONS_PREFIX + _b64(self.machine_permissions.encode("utf-8")))
        if self.machine_accounts:
            key_content = self.remote_files.get(variable_by_name["_lk_key_file"])
            if key_content is None or not key_content.strip():
                return CommandResult(
                    stdout=f"{_outcome_marker(_MachineScriptOutcome.KEY_MISSING)}\n", stderr="", success=True
                )
            lines.append(_READ_CREDENTIALS_PREFIX + _b64(store_document(self.machine_accounts)))
            lines.append(_READ_DATA_FORMAT_VERSION_PREFIX + _b64(b"2"))
        lines.append(_outcome_marker(_MachineScriptOutcome.APPLIED))
        return CommandResult(stdout="\n".join(lines) + "\n", stderr="", success=True)

    def _clear_account(self, service_name: str, account: str) -> None:
        remaining = [entry for entry in self.machine_accounts.get(service_name, []) if entry != account]
        if remaining:
            self.machine_accounts[service_name] = remaining
        else:
            self.machine_accounts.pop(service_name, None)

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        self.written.append(WrittenFile(path=str(path), content=content, mode=mode, is_atomic=is_atomic))
        self.remote_files[str(path)] = content

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
    ) -> None:
        self.written.append(WrittenFile(path=str(path), content=content.encode(encoding), mode=mode))


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _script_encryption_key(script: str) -> str | None:
    """Return the literal LATCHKEY_ENCRYPTION_KEY a verification script carries, if any."""
    for line in script.splitlines():
        if line.startswith("LATCHKEY_ENCRYPTION_KEY=") and "$(" not in line:
            return shlex.split(line.split("=", 1)[1])[0]
    return None


def machine_script_variables(outer: OuterHostInterface) -> dict[str, str]:
    """The ``_lk_*`` payload variables of the last machine script this stub was handed."""
    return _parse_script_variables(as_stub(outer).recorded[-1].command)


def machine_script_line(outer: OuterHostInterface, marker: str) -> str:
    """The one line of the last machine script containing ``marker``, for asserting on what it told the CLI."""
    matching = [line for line in as_stub(outer).recorded[-1].command.splitlines() if marker in line]
    assert len(matching) == 1, f"expected exactly one line containing {marker!r}, found {len(matching)}"
    return matching[0]


def _parse_script_variables(script: str) -> dict[str, str]:
    """Read the ``_lk_*`` assignments a consolidated script opens with, shell-unquoted."""
    variable_by_name: dict[str, str] = {}
    for line in script.splitlines():
        match = _SCRIPT_VARIABLE_LINE.match(line)
        if match is None:
            continue
        variable_by_name[match.group(1)] = "".join(shlex.split(match.group(2)))
    return variable_by_name


def stub_outer(result: CommandResult, name: str = "vps-test") -> OuterHostInterface:
    """Build a stub outer host typed as ``OuterHostInterface``.

    ``cast`` is used because the stub is structurally-but-not-nominally an
    OuterHostInterface (the interface has many other abstract methods that the
    function under test never calls).
    """
    return cast(OuterHostInterface, StubOuter(name=name, result=result))


def as_stub(outer: OuterHostInterface) -> StubOuter:
    return cast(StubOuter, outer)


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
    "directory = os.environ['LATCHKEY_DIRECTORY']\n"
    "argv = sys.argv[1:]\n"
    "data = load(directory)\n"
    "if argv[:2] == ['auth', 'list']:\n"
    "    out = {name: accounts_of(data, name) for name in data['accounts']}\n"
    "elif argv[:2] == ['services', 'info']:\n"
    "    out = {'credentials': accounts_of(data, argv[2])}\n"
    "elif argv[:2] == ['auth', 'clear']:\n"
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
    # Upstream's wording (latchkey 3.10.1), repeated rather than imported: the
    # script under test copes with what the real CLI prints, so the fake has to
    # print that and not whatever the script happens to look for.
    "    if not data['accounts'] and '--services' not in argv:\n"
    "        sys.exit('Error: No stored credentials found to re-encrypt.')\n"
    "    destination, rest = argv[2], argv[3:]\n"
    "    account = None\n"
    "    if '--account' in rest:\n"
    "        account = rest[rest.index('--account') + 1]\n"
    "        rest = rest[: rest.index('--account')]\n"
    "    wanted = rest[1:] if rest[:1] == ['--services'] else None\n"
    "    merged = dict(load(destination)['accounts'])\n"
    "    for name, accounts in data['accounts'].items():\n"
    "        if wanted is not None and name not in wanted:\n"
    "            continue\n"
    "        if account is None:\n"
    "            merged[name] = sorted(accounts)\n"
    "        elif account in accounts:\n"
    "            merged[name] = sorted(set(merged.get(name, [])) | {account})\n"
    "    os.makedirs(destination, exist_ok=True)\n"
    "    with open(os.path.join(destination, STORE), 'w') as handle:\n"
    "        json.dump({'accounts': merged, 'key': sys.stdin.read().strip() or data['key']}, handle)\n"
    "    out = None\n"
    "else:\n"
    "    sys.exit('unsupported invocation: ' + ' '.join(argv))\n"
    "if out is not None:\n"
    "    sys.stdout.write(json.dumps(out))\n"
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


def stub_machine(
    accounts_by_service: Mapping[str, Sequence[str]] | None = None,
    machine_permissions: str | None = None,
) -> OuterHostInterface:
    """A stub VPS holding a credential store -- and optionally a policy -- of its own."""
    return cast(
        OuterHostInterface,
        StubOuter(
            result=CommandResult(stdout="", stderr="", success=True),
            machine_accounts={name: list(accounts) for name, accounts in (accounts_by_service or {}).items()},
            machine_permissions=machine_permissions,
        ),
    )
