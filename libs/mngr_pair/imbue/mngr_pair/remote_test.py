"""Unit tests for ``remote.py`` -- unison roots, SSH transport, and version gating."""

import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_pair.remote import MINIMUM_UNISON_VERSION
from imbue.mngr_pair.remote import REMOTE_UNISON_SHA256
from imbue.mngr_pair.remote import REMOTE_UNISON_VERSION
from imbue.mngr_pair.remote import SshEndpoint
from imbue.mngr_pair.remote import UnisonRoot
from imbue.mngr_pair.remote import UnisonVersionError
from imbue.mngr_pair.remote import build_remote_unison_script
from imbue.mngr_pair.remote import check_local_unison_version
from imbue.mngr_pair.remote import ensure_remote_unison
from imbue.mngr_pair.remote import is_version_compatible
from imbue.mngr_pair.remote import parse_unison_version
from imbue.mngr_pair.remote import write_ssh_wrapper_script

# =============================================================================
# Test: version parsing and the compatibility floor
# =============================================================================


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("unison version 2.54.0 (ocaml 5.4.1)", (2, 54)),
        ("unison version 2.51.5 (ocaml 4.13.1)", (2, 51)),
        ("unison version 2.53.8\n", (2, 53)),
        ("some unrelated output", None),
        ("", None),
    ],
)
def test_parse_unison_version_reads_major_minor(output: str, expected: tuple[int, int] | None) -> None:
    assert parse_unison_version(output) == expected


@pytest.mark.parametrize(
    ("version", "is_compatible"),
    [
        ((2, 54), True),
        ((2, 52), True),
        # 2.51 has no interop at all with a client newer than 2.53.8, which is why
        # presence of the binary is not enough to go on.
        ((2, 51), False),
        ((2, 48), False),
        ((3, 0), True),
        (None, False),
    ],
)
def test_is_version_compatible_enforces_the_interop_floor(
    version: tuple[int, int] | None, is_compatible: bool
) -> None:
    assert is_version_compatible(version) is is_compatible


def test_minimum_version_is_the_documented_interop_boundary() -> None:
    """2.52 is where unison gained cross-version interop; below it there is none."""
    assert MINIMUM_UNISON_VERSION == (2, 52)


# =============================================================================
# Test: UnisonRoot rendering
# =============================================================================


def test_local_root_renders_as_a_bare_path() -> None:
    root = UnisonRoot(path=Path("/home/me/repo"))
    assert root.as_root_arg() == "/home/me/repo"
    assert root.is_remote is False


def test_remote_root_renders_as_ssh_url_with_doubled_slash() -> None:
    """The doubled slash is unison's absolute-path marker; one slash means $HOME-relative."""
    endpoint = SshEndpoint(user="root", hostname="10.0.0.4", port=2222, key_path=Path("/keys/id"))
    root = UnisonRoot(path=Path("/work/repo"), ssh=endpoint)

    assert root.as_root_arg() == "ssh://root@10.0.0.4//work/repo"
    assert root.is_remote is True


def test_remote_root_omits_the_port_because_the_syntax_has_no_place_for_it() -> None:
    """The port travels via -sshcmd instead; putting it in the root would be silently wrong."""
    endpoint = SshEndpoint(user="agent", hostname="host.example", port=2222, key_path=Path("/keys/id"))
    root = UnisonRoot(path=Path("/work"), ssh=endpoint)

    assert "2222" not in root.as_root_arg()


def test_remote_root_rejects_a_relative_path() -> None:
    """A relative remote root renders as $HOME-relative, i.e. quietly the wrong directory."""
    endpoint = SshEndpoint(user="root", hostname="h", port=22, key_path=Path("/keys/id"))

    with pytest.raises(MngrError) as exc_info:
        UnisonRoot(path=Path("work/repo"), ssh=endpoint)

    assert "absolute path" in str(exc_info.value)


def test_local_root_accepts_a_relative_path() -> None:
    """``mngr pair --target ./local-dir`` passes the path through as typed."""
    assert UnisonRoot(path=Path("local-dir")).as_root_arg() == "local-dir"


# =============================================================================
# Test: SshEndpoint construction
# =============================================================================


def test_ssh_endpoint_is_none_for_a_local_host() -> None:
    assert SshEndpoint.from_host(cast(OnlineHostInterface, FakeHost(is_local=True))) is None


def test_ssh_endpoint_refuses_a_remote_host_with_no_connection_info() -> None:
    """Treating "no endpoint" as "local" would sync a directory on this machine instead."""
    host = cast(OnlineHostInterface, FakeHost(is_local=False, ssh_info=None))

    with pytest.raises(MngrError) as exc_info:
        SshEndpoint.from_host(host)

    assert "SSH connection info" in str(exc_info.value)


# =============================================================================
# Test: the generated ssh wrapper
# =============================================================================


def test_ssh_wrapper_survives_a_key_path_containing_spaces(tmp_path: Path) -> None:
    """The wrapper exists precisely because unison splits -sshargs on bare whitespace.

    Running the generated script with a stand-in for ssh proves the quoting holds:
    a key path with a space must arrive as ONE argument, and unison's own trailing
    arguments must be appended after it.
    """
    key_path = tmp_path / "Jane Doe" / "id_ed25519"
    key_path.parent.mkdir()
    key_path.touch()
    endpoint = SshEndpoint(user="root", hostname="h", port=2200, key_path=key_path)

    wrapper = write_ssh_wrapper_script(endpoint, tmp_path / "wrapper")

    # Stand in for the real ssh binary and dump argv one entry per line.
    fake_ssh_dir = tmp_path / "bin"
    fake_ssh_dir.mkdir()
    fake_ssh = fake_ssh_dir / "ssh"
    fake_ssh.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    fake_ssh.chmod(0o755)

    result = subprocess.run(
        [str(wrapper), "-l", "root", "somehost", "-e", "none"],
        capture_output=True,
        text=True,
        env={"PATH": f"{fake_ssh_dir}:/usr/bin:/bin"},
    )

    argv = result.stdout.splitlines()
    # One argument, not three. This is the property -sshargs cannot provide.
    assert str(key_path) in argv, f"key path was split or mangled: {argv}"
    # unison's own arguments are appended verbatim, after mngr's options.
    assert argv[-5:] == ["-l", "root", "somehost", "-e", "none"], f"appended arguments were mangled: {argv}"
    assert argv.index(str(key_path)) < argv.index("somehost"), "mngr's options must precede unison's"


def test_ssh_wrapper_is_executable_and_pins_host_key_checking(tmp_path: Path) -> None:
    endpoint = SshEndpoint(
        user="root",
        hostname="h",
        port=22,
        key_path=tmp_path / "id",
        known_hosts_path=tmp_path / "known_hosts",
    )

    wrapper = write_ssh_wrapper_script(endpoint, tmp_path / "wrapper")
    body = wrapper.read_text()

    assert wrapper.stat().st_mode & 0o100, "wrapper must be executable"
    assert body.startswith("#!/bin/sh")
    assert body.rstrip().endswith('"$@"'), "unison appends its own arguments"
    assert "StrictHostKeyChecking=yes" in body
    # Without these, a BatchMode child can hang on the macOS agent socket.
    assert "IdentitiesOnly=yes" in body
    assert "IdentityAgent=none" in body


# =============================================================================
# Test: the remote provisioning script
# =============================================================================


def test_remote_script_pins_version_and_checksum() -> None:
    script = build_remote_unison_script()

    assert REMOTE_UNISON_VERSION in script
    assert REMOTE_UNISON_SHA256 in script
    assert f"v{REMOTE_UNISON_VERSION}" in script, "download URL must reference the pinned tag"


def test_remote_script_prefers_an_existing_unison_over_downloading() -> None:
    """An already-compatible unison on the host should short-circuit the install."""
    script = build_remote_unison_script()

    probe_index = script.index("if command -v unison >")
    download_index = script.index("curl")
    assert probe_index < download_index


def test_remote_script_refuses_unsupported_platforms_rather_than_guessing() -> None:
    """The pinned asset is a static Linux x86_64 ELF, so both halves have to be checked.

    Upstream ships no aarch64 Linux build to fall back to, and the OS matters as much
    as the architecture: an x86_64 macOS host must not be handed a Linux binary.
    """
    script = build_remote_unison_script()

    assert "uname -s" in script
    assert "uname -m" in script
    assert "MNGR_UNISON_UNSUPPORTED" in script


def test_remote_script_verifies_the_download_before_installing() -> None:
    script = build_remote_unison_script()

    checksum_index = min(script.index("sha256sum -c"), script.index("shasum -a 256 -c"))
    install_index = script.index("install -m 0755")
    assert checksum_index < install_index, "checksum must be verified before anything is installed"


def test_remote_script_gate_tracks_the_python_constant() -> None:
    """The shell gate must be derived from MINIMUM_UNISON_VERSION, not hardcoded.

    The floor is enforced twice -- once in Python for the local side, once in sh for
    the remote side -- so a hardcoded copy would silently keep gating at the old
    version after a bump.
    """
    major, minor = MINIMUM_UNISON_VERSION
    script = build_remote_unison_script()

    assert f'[ "$maj" -gt {major} ]' in script
    assert f'[ "$min" -ge {minor} ]' in script


# =============================================================================
# Test: the remote provisioning script, actually executed
# =============================================================================


def _write_stub(directory: Path, name: str, body: str) -> Path:
    """Write an executable POSIX-sh stub into ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / name
    # Replace rather than write through: a name that is also linked to a real utility
    # below would otherwise have the write follow the symlink out of the sandbox.
    stub.unlink(missing_ok=True)
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    return stub


def _uname_stub_body(os_name: str, machine: str) -> str:
    """Body for a ``uname`` stub that answers ``-s`` and ``-m`` separately."""
    return f'case "$1" in -s) echo {shlex.quote(os_name)} ;; *) echo {shlex.quote(machine)} ;; esac'


def _link_real_utilities(stub_dir: Path, names: tuple[str, ...]) -> None:
    """Link the real utilities the script (or a stub of it) shells out to into the sandbox."""
    stub_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        resolved = shutil.which(name)
        if resolved is not None:
            (stub_dir / name).symlink_to(resolved)


def _make_provisioning_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """Build the ``PATH`` directory and fake ``HOME`` for a run of the script.

    The sandbox PATH holds nothing but the stubs and the handful of real utilities
    the script needs, so no unison installed on the machine running the tests can
    leak in and change which rung of the ladder is taken. ``curl`` is stubbed to
    fail and leave a marker file, which keeps a run that goes wrong off the network
    and lets a test assert that nothing was downloaded. The marker is created by a
    bare redirection rather than ``touch``, because this PATH holds no ``touch``.
    """
    stub_dir = tmp_path / "stubs"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _write_stub(stub_dir, "curl", f"> {shlex.quote(str(tmp_path / 'curl-ran'))}\nexit 1")
    _link_real_utilities(stub_dir, ("awk", "printf"))
    return stub_dir, home


def _make_install_sandbox(tmp_path: Path, installed_unison_body: str) -> tuple[Path, Path]:
    """A sandbox that reaches the download rung and installs a stub as the new unison.

    Nothing usable is on PATH and no mngr copy exists, so the ladder falls through to
    the install. ``uname`` reports a supported platform and ``curl`` fabricates a
    tarball laid out like the upstream one, whose ``bin/unison`` is
    ``installed_unison_body``.

    The checksum check is stubbed to pass because the script pins the checksum of the
    real 2.1 MB asset, which a fabricated tarball cannot match and which this test has
    no business downloading; that the gate runs before anything is installed has its
    own test above.
    """
    stub_dir = tmp_path / "stubs"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    # ``gzip`` is needed even though the script never names it: GNU tar shells out to
    # it by name for ``-z``, where macOS's bsdtar decompresses in-process. Without it
    # these tests pass on a developer's Mac and fail on Linux CI.
    _link_real_utilities(stub_dir, ("awk", "printf", "mktemp", "mkdir", "cp", "tar", "gzip", "rm", "install", "mv"))
    _write_stub(stub_dir, "uname", _uname_stub_body("Linux", "x86_64"))
    _write_stub(stub_dir, "sha256sum", "exit 0")
    _write_stub(stub_dir, "shasum", "exit 0")
    payload = _write_stub(tmp_path / "payload", "unison", installed_unison_body)
    _write_stub(
        stub_dir,
        "curl",
        f"""out=""
while [ $# -gt 0 ]; do
    case "$1" in -o) out="$2"; shift 2 ;; *) shift ;; esac
done
stage=$(mktemp -d)
mkdir -p "$stage/unison-pkg/bin"
cp {shlex.quote(str(payload))} "$stage/unison-pkg/bin/unison"
cp {shlex.quote(str(payload))} "$stage/unison-pkg/bin/unison-fsmonitor"
tar -czf "$out" -C "$stage" unison-pkg
rm -rf "$stage"
""",
    )
    return stub_dir, home


def _run_provisioning_script(stub_dir: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Run the generated script in the sandbox and capture what it reports."""
    return subprocess.run(
        ["/bin/sh", "-c", build_remote_unison_script()],
        capture_output=True,
        text=True,
        env={"PATH": str(stub_dir), "HOME": str(home)},
    )


def test_running_the_script_reports_a_usable_unison_already_on_path(tmp_path: Path) -> None:
    """A host that has both binaries is used as-is, and nothing is fetched.

    The watcher here is only on PATH, not beside the unison -- one of the two places
    unison looks for it.
    """
    stub_dir, home = _make_provisioning_sandbox(tmp_path)
    unison = _write_stub(stub_dir, "unison", 'echo "unison version 2.54.0 (ocaml 5.4.1)"')
    _write_stub(stub_dir, "unison-fsmonitor", "exit 0")

    result = _run_provisioning_script(stub_dir, home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"MNGR_UNISON {unison} 2.54.0"
    assert not (tmp_path / "curl-ran").exists(), "a usable unison must short-circuit the download"


def test_running_the_script_reuses_a_previously_installed_copy_at_the_exact_floor(tmp_path: Path) -> None:
    """Second rung: ``~/.mngr/bin/unison``, accepted at exactly the version floor.

    Its watcher sits beside it rather than on PATH, which is how the install rung
    leaves things -- the other place unison looks.
    """
    stub_dir, home = _make_provisioning_sandbox(tmp_path)
    installed = _write_stub(home / ".mngr" / "bin", "unison", 'echo "unison version 2.52.1 (ocaml 4.14.0)"')
    _write_stub(home / ".mngr" / "bin", "unison-fsmonitor", "exit 0")

    result = _run_provisioning_script(stub_dir, home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"MNGR_UNISON {installed} 2.52.1"
    assert not (tmp_path / "curl-ran").exists(), "an installed copy must short-circuit the download"


def test_running_the_script_rejects_a_new_enough_unison_that_has_no_file_watcher(tmp_path: Path) -> None:
    """Continuous sync needs a watcher on the far side, so version alone is not enough.

    This is the shape every Debian and Ubuntu host has: the packaged unison is new
    enough but ships no ``unison-fsmonitor``, and neither archive has one to install.
    Accepting it would let the pairing complete one sync and then die with
    "Server: No file monitoring helper program found".
    """
    stub_dir, home = _make_provisioning_sandbox(tmp_path)
    _write_stub(stub_dir, "unison", 'echo "unison version 2.53.7 (ocaml 5.2.0)"')
    _write_stub(stub_dir, "uname", _uname_stub_body("Linux", "aarch64"))

    result = _run_provisioning_script(stub_dir, home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MNGR_UNISON_UNSUPPORTED Linux/aarch64"
    assert not (tmp_path / "curl-ran").exists(), "there is nothing to download for this platform"


def test_running_the_script_rejects_the_ubuntu_2204_unison_and_names_the_platform(tmp_path: Path) -> None:
    """2.51.5 cannot interoperate, and on Linux aarch64 there is no upstream build to install."""
    stub_dir, home = _make_provisioning_sandbox(tmp_path)
    _write_stub(stub_dir, "unison", 'echo "unison version 2.51.5 (ocaml 4.13.1)"')
    # A watcher is present, so the version is the only thing left to reject it on.
    _write_stub(stub_dir, "unison-fsmonitor", "exit 0")
    _write_stub(stub_dir, "uname", _uname_stub_body("Linux", "aarch64"))

    result = _run_provisioning_script(stub_dir, home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MNGR_UNISON_UNSUPPORTED Linux/aarch64"
    assert not (tmp_path / "curl-ran").exists(), "there is nothing to download for this platform"


def test_running_the_script_refuses_a_non_linux_host_on_a_supported_architecture(tmp_path: Path) -> None:
    """An x86_64 macOS host must not be given the Linux static build to exec."""
    stub_dir, home = _make_provisioning_sandbox(tmp_path)
    _write_stub(stub_dir, "uname", _uname_stub_body("Darwin", "x86_64"))

    result = _run_provisioning_script(stub_dir, home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MNGR_UNISON_UNSUPPORTED Darwin/x86_64"
    assert not (tmp_path / "curl-ran").exists(), "a Linux-only build must not be downloaded here"


def test_running_the_script_installs_and_reports_what_it_installed(tmp_path: Path) -> None:
    """The download rung, end to end: both binaries land in ~/.mngr/bin and get reported."""
    stub_dir, home = _make_install_sandbox(tmp_path, 'echo "unison version 2.54.0 (ocaml 5.4.1)"')

    result = _run_provisioning_script(stub_dir, home)

    installed = home / ".mngr" / "bin" / "unison"
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"MNGR_UNISON {installed} 2.54.0"
    assert installed.is_file()
    # unison-fsmonitor comes from the same tarball; -repeat watch needs it on macOS.
    assert (home / ".mngr" / "bin" / "unison-fsmonitor").is_file()


def test_running_the_script_fails_when_the_binary_it_installed_cannot_run(tmp_path: Path) -> None:
    """A download that cannot execute here must fail now, not inside unison's protocol.

    The installed binary is checked by running it, exactly like a candidate that was
    already present -- otherwise mngr would report a ``-servercmd`` path that only
    fails much later, with an error that says nothing about the real cause.
    """
    stub_dir, home = _make_install_sandbox(tmp_path, "exit 126")

    result = _run_provisioning_script(stub_dir, home)

    assert result.returncode != 0
    assert "does not run on this host" in result.stderr
    assert "MNGR_UNISON" not in result.stdout


def test_running_the_script_ignores_a_present_but_broken_unison(tmp_path: Path) -> None:
    """Why there is no version stamp: a stamp would claim a crashing binary is fine."""
    stub_dir, home = _make_provisioning_sandbox(tmp_path)
    _write_stub(stub_dir, "unison", "exit 127")
    _write_stub(stub_dir, "unison-fsmonitor", "exit 0")
    _write_stub(stub_dir, "uname", _uname_stub_body("Linux", "aarch64"))

    result = _run_provisioning_script(stub_dir, home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MNGR_UNISON_UNSUPPORTED Linux/aarch64"


# =============================================================================
# Test: ensure_remote_unison result handling
# =============================================================================


class _ScriptedHost(FakeHost):
    """A host whose command execution returns canned output, for the resolver paths."""

    canned_stdout: str = ""
    is_canned_success: bool = True

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        return CommandResult(stdout=self.canned_stdout, stderr="boom", success=self.is_canned_success)


def _host_returning(stdout: str, is_success: bool = True) -> OnlineHostInterface:
    return cast(OnlineHostInterface, _ScriptedHost(canned_stdout=stdout, is_canned_success=is_success))


def test_ensure_remote_unison_returns_the_resolved_path() -> None:
    host = _host_returning("MNGR_UNISON /opt/unison/bin/unison 2.53.3\n")

    assert ensure_remote_unison(host) == Path("/opt/unison/bin/unison")


def test_ensure_remote_unison_ignores_unrelated_output_before_the_result_line() -> None:
    """The script may emit warnings; only the tagged line is the result."""
    host = _host_returning("some noise\nMNGR_UNISON /root/.mngr/bin/unison 2.54.0\n")

    assert ensure_remote_unison(host) == Path("/root/.mngr/bin/unison")


def test_ensure_remote_unison_explains_an_unsupported_architecture() -> None:
    """Upstream has no aarch64 build, so the error must name the arch and say what to do."""
    host = _host_returning("MNGR_UNISON_UNSUPPORTED aarch64\n")

    with pytest.raises(UnisonVersionError) as exc_info:
        ensure_remote_unison(host)

    assert "aarch64" in str(exc_info.value)


def test_ensure_remote_unison_raises_when_the_script_fails() -> None:
    host = _host_returning("", is_success=False)

    with pytest.raises(UnisonVersionError):
        ensure_remote_unison(host)


def test_ensure_remote_unison_raises_when_no_result_line_is_emitted() -> None:
    """A silent success would otherwise return a meaningless path."""
    host = _host_returning("unexpected output with no tagged line\n")

    with pytest.raises(UnisonVersionError):
        ensure_remote_unison(host)


# =============================================================================
# Test: the local version gate
# =============================================================================


@pytest.mark.unison
def test_check_local_unison_version_accepts_the_installed_unison(cg: ConcurrencyGroup) -> None:
    """The floor is a real requirement, so a machine that can run these tests must meet it."""
    assert check_local_unison_version(cg) >= MINIMUM_UNISON_VERSION
