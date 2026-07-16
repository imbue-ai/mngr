"""Acceptance test proving the manifest-pinned dugite-native git payload end to end.

Downloads the pinned release asset for the current platform (per
``scripts/git-manifest.json``), recomputes its SHA256 against the manifest pin (a
live supply-chain tripwire), extracts it, and exercises the payload under the
exact runtime environment contract that ``electron/backend.js`` exports:

- ``git --version`` must report exactly the manifest's ``gitVersion``;
- a local ``git clone`` must succeed with no "templates not found" warning
  (the regression signal for ``GIT_TEMPLATE_DIR``);
- an HTTPS clone against a local self-signed TLS server must succeed, proving
  ``git-remote-https`` helper dispatch and TLS wiring. This is the historical
  failure mode: the dugite-native binaries are built with an empty prefix, so
  without ``GIT_EXEC_PATH`` the exec-path resolves to ``//libexec/git-core`` and
  https clones fail with "'remote-https' is not a git command";
- the same must hold after ``scripts/download-binaries.js`` replaces the
  payload's symlinks with sh shims (the shape the app actually ships --
  symlinks would be materialized into ~480MB of binary copies by ToDesktop's
  app-source zip): zero symlinks remain, an HTTPS clone still works (the
  ``git-remote-https`` shim), and dashed-form dispatch still works (a
  ``libexec/git-core/git-fetch`` shim invocation).

Everything is hermetic except obtaining the release asset: the HTTPS clone runs
against a 127.0.0.1 server on an OS-assigned port, serving a dumb-HTTP bare repo
with a certificate minted by this test and trusted via ``GIT_SSL_CAINFO``. In
offload CI even the asset is hermetic -- sandboxes have no network, so the mngr
Dockerfile pre-fetches it into ``MINDS_DUGITE_NATIVE_CACHE_DIR`` at image build
time; on dev machines the test downloads it live instead.

Design: specs/minds-managed-git/concise.md. Runs on linux-x64 in offload CI and
on macs locally; skips on platforms with no mapped manifest target (e.g. Windows).

Run from the repo root:
    just test apps/minds/test_bundled_git.py::test_bundled_git_payload_end_to_end
"""

import hashlib
import http.server
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import threading
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any
from typing import Final

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.imbue_common.logging import log_span

pytestmark = pytest.mark.acceptance

_MANIFEST_PATH: Final[Path] = Path(__file__).resolve().parent / "scripts" / "git-manifest.json"
_DOWNLOAD_BINARIES_SCRIPT_PATH: Final[Path] = Path(__file__).resolve().parent / "scripts" / "download-binaries.js"

# Maps (sys.platform, platform.machine()) to a git-manifest.json target key.
# Anything unmapped (e.g. Windows) skips the test.
_TARGET_KEY_BY_PLATFORM_AND_MACHINE: Final[dict[tuple[str, str], str]] = {
    ("darwin", "arm64"): "darwin-arm64",
    ("darwin", "x86_64"): "darwin-x64",
    ("linux", "x86_64"): "linux-x64",
    ("linux", "aarch64"): "linux-arm64",
}

# Stall timeout per download attempt (httpx applies it to each connect/read
# operation, not the whole body), and a hard timeout per git invocation.
_DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 180.0
_GIT_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0

_TEST_IDENTITY_NAME: Final[str] = "Bundled Git Acceptance Test"
_TEST_IDENTITY_EMAIL: Final[str] = "bundled-git-test@imbue.com"


class _StaticFileRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files from ``directory_root`` without per-request logging.

    A dumb-HTTP git fetch is a series of plain GETs (info/refs, HEAD, loose
    objects), so a static file server is all git-remote-https needs on the
    other end.
    """

    # Set before the server starts; handler instances are constructed per request.
    directory_root: str = "."

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=type(self).directory_root, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        return None


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _download_release_asset(asset_url: str) -> bytes:
    """Download a GitHub release asset (following the redirect to its CDN), retrying transport/HTTP errors."""
    response = httpx.get(asset_url, follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.content


def _build_git_environment(git_root: Path, fake_home: Path) -> dict[str, str]:
    """Build the isolated environment contract mirroring what electron/backend.js exports.

    Only the tail of the host PATH is reused (git needs sh/ssh resolvable);
    HOME, config files, and identity are pinned to test-owned locations so the
    host machine's git configuration cannot leak in.
    """
    base_environment = {
        "PATH": f"{git_root / 'bin'}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "GIT_EXEC_PATH": str(git_root / "libexec" / "git-core"),
        "GIT_TEMPLATE_DIR": str(git_root / "share" / "git-core" / "templates"),
        "GIT_CONFIG_SYSTEM": str(git_root / "etc" / "gitconfig"),
        "HOME": str(fake_home),
        "XDG_CONFIG_HOME": str(fake_home / ".config"),
        "GIT_CONFIG_GLOBAL": str(fake_home / "gitconfig-global"),
        "GIT_AUTHOR_NAME": _TEST_IDENTITY_NAME,
        "GIT_AUTHOR_EMAIL": _TEST_IDENTITY_EMAIL,
        "GIT_COMMITTER_NAME": _TEST_IDENTITY_NAME,
        "GIT_COMMITTER_EMAIL": _TEST_IDENTITY_EMAIL,
        "GIT_TERMINAL_PROMPT": "0",
    }
    # The Linux payload does not use the system trust store, so the contract
    # points at the bundled CA file (macOS links the system libcurl instead).
    linux_only_environment = (
        {"GIT_SSL_CAINFO": str(git_root / "ssl" / "cacert.pem")} if sys.platform == "linux" else {}
    )
    return {**base_environment, **linux_only_environment}


def _run_git(
    git_binary: Path,
    git_args: Sequence[str],
    environment: Mapping[str, str],
    working_dir: Path,
) -> subprocess.CompletedProcess[str]:
    """Run one git command under the payload environment, asserting it exits 0."""
    command = [str(git_binary), *git_args]
    result = subprocess.run(
        command,
        env=dict(environment),
        cwd=working_dir,
        capture_output=True,
        text=True,
        timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"git command failed with exit code {result.returncode}: {command}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def _convert_payload_symlinks_to_shims(git_root: Path) -> int:
    """Run the production shim conversion from scripts/download-binaries.js on an extracted payload.

    Invokes the same exported function the build and the ToDesktop
    ``beforeInstall`` hook run, so this test exercises the exact payload shape
    the app ships. Node is a hard requirement of the minds toolchain (the
    offload image provisions it for exactly this kind of ``node -e`` use).
    """
    node_binary = shutil.which("node")
    assert node_binary is not None, (
        "node is required to run the shim conversion from scripts/download-binaries.js; "
        "it is provisioned in the offload image and by the minds dev setup (apps/minds/.nvmrc)"
    )
    conversion_script = (
        "const converted = require(process.argv[1]).convertGitPayloadSymlinksToShims(process.argv[2]);"
        "console.log(converted);"
    )
    result = subprocess.run(
        [node_binary, "-e", conversion_script, str(_DOWNLOAD_BINARIES_SCRIPT_PATH), str(git_root)],
        capture_output=True,
        text=True,
        timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"convertGitPayloadSymlinksToShims failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return int(result.stdout.strip())


def _generate_self_signed_localhost_certificate(certificate_dir: Path) -> tuple[Path, Path]:
    """Mint a short-lived self-signed certificate for localhost/127.0.0.1; returns (certificate_path, key_path)."""
    certificate_dir.mkdir(parents=True, exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject_name)
        .issuer_name(subject_name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    certificate_path = certificate_dir / "server-certificate.pem"
    private_key_path = certificate_dir / "server-private-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return certificate_path, private_key_path


@contextmanager
def _serve_directory_over_https(
    serve_root: Path,
    certificate_path: Path,
    private_key_path: Path,
) -> Iterator[str]:
    """Serve ``serve_root`` over HTTPS on 127.0.0.1 with an OS-assigned port; yields the base URL."""
    _StaticFileRequestHandler.directory_root = str(serve_root)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StaticFileRequestHandler)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=certificate_path, keyfile=private_key_path)
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    serving_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serving_thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        serving_thread.join(timeout=10)


@pytest.mark.timeout(540)
def test_bundled_git_payload_end_to_end(tmp_path: Path) -> None:
    """Download, hash-verify, extract, and exercise the pinned git payload for this platform."""
    # Resolve the manifest target for the current platform; unmapped platforms
    # (e.g. Windows) have no dugite-native payload to verify here.
    machine = platform.machine()
    target_key = _TARGET_KEY_BY_PLATFORM_AND_MACHINE.get((sys.platform, machine))
    if target_key is None:
        pytest.skip(f"no dugite-native manifest target for platform={sys.platform!r} machine={machine!r}")

    manifest: dict[str, Any] = json.loads(_MANIFEST_PATH.read_text())
    target: dict[str, Any] = manifest["targets"][target_key]
    asset_name: str = target["asset"]
    expected_sha256: str = target["sha256"]
    pinned_git_version: str = manifest["gitVersion"]
    dugite_tag: str = manifest["dugiteNativeTag"]

    # Phase 1: obtain the pinned release asset -- from the image cache when
    # present (offload sandboxes have no network), else a live download -- and
    # recompute its SHA256 against the manifest pin, whichever the source.
    cache_dir_value = os.environ.get("MINDS_DUGITE_NATIVE_CACHE_DIR")
    cached_asset_path = Path(cache_dir_value) / asset_name if cache_dir_value else None
    if cached_asset_path is not None and cached_asset_path.is_file():
        with log_span("Reading pinned dugite-native asset from image cache {}", cached_asset_path):
            asset_bytes = cached_asset_path.read_bytes()
    else:
        asset_url = f"https://github.com/desktop/dugite-native/releases/download/{dugite_tag}/{asset_name}"
        with log_span("Downloading pinned dugite-native asset {}", asset_url):
            asset_bytes = _download_release_asset(asset_url)
    actual_sha256 = hashlib.sha256(asset_bytes).hexdigest()
    assert actual_sha256 == expected_sha256, (
        f"SHA256 mismatch for {asset_name}: the manifest pins {expected_sha256} but the downloaded bytes "
        f"hash to {actual_sha256}. Either the manifest hash is wrong or the release asset changed upstream."
    )

    # Phase 2: extract the payload with the safe filter. The tarball is rooted
    # flat: bin/, etc/, libexec/, share/ (and ssl/ on linux) at the archive root.
    archive_path = tmp_path / asset_name
    archive_path.write_bytes(asset_bytes)
    git_root = tmp_path / "git"
    with log_span("Extracting {} into {}", asset_name, git_root):
        with tarfile.open(archive_path) as archive:
            archive.extractall(git_root, filter="data")
    git_binary = git_root / "bin" / "git"
    extracted_root_entries = sorted(entry.name for entry in git_root.iterdir())
    assert git_binary.is_file(), f"payload is missing bin/git; extracted archive root: {extracted_root_entries}"
    assert (git_root / "libexec" / "git-core" / "git-remote-https").is_file(), (
        "payload is missing libexec/git-core/git-remote-https, so no https transport is possible"
    )

    # Build the runtime environment contract, fully isolated from the host.
    fake_home = tmp_path / "home"
    (fake_home / ".config").mkdir(parents=True)
    base_environment = _build_git_environment(git_root=git_root, fake_home=fake_home)

    # Phase 3: the payload must report exactly the manifest's pinned version,
    # regardless of which machine runs it.
    version_result = _run_git(git_binary, ("--version",), base_environment, tmp_path)
    assert version_result.stdout.strip() == f"git version {pinned_git_version}", (
        f"bundled git reported {version_result.stdout.strip()!r}, but the manifest pins "
        f"git version {pinned_git_version!r}"
    )

    # Phase 4: local round trip -- init a fixture repo with one commit, clone it
    # over file://, and require that neither init nor clone warned about missing
    # templates (the GIT_TEMPLATE_DIR regression signal).
    fixture_repo = tmp_path / "fixture-repo"
    fixture_repo.mkdir()
    init_result = _run_git(git_binary, ("init", "--initial-branch=main"), base_environment, fixture_repo)
    (fixture_repo / "README.md").write_text("bundled git payload fixture\n")
    _run_git(git_binary, ("add", "README.md"), base_environment, fixture_repo)
    _run_git(git_binary, ("commit", "-m", "initial commit"), base_environment, fixture_repo)
    fixture_head = _run_git(git_binary, ("rev-parse", "HEAD"), base_environment, fixture_repo).stdout.strip()

    local_clone = tmp_path / "local-clone"
    local_clone_result = _run_git(
        git_binary, ("clone", f"file://{fixture_repo}", str(local_clone)), base_environment, tmp_path
    )
    assert (local_clone / "README.md").is_file(), "file:// clone did not materialize the fixture worktree"
    for round_trip_result in (init_result, local_clone_result):
        assert "templates not found" not in round_trip_result.stderr, (
            f"git warned about missing templates -- GIT_TEMPLATE_DIR is wired wrong:\n{round_trip_result.stderr}"
        )

    # Phase 5: hermetic HTTPS clone through git-remote-https. Serve a bare
    # dumb-HTTP clone of the fixture from a local TLS server with a certificate
    # minted above, trusted for this one invocation via GIT_SSL_CAINFO. Without
    # a working GIT_EXEC_PATH this is exactly where "'remote-https' is not a git
    # command" appears.
    serve_root = tmp_path / "www"
    serve_root.mkdir()
    bare_repo = serve_root / "fixture.git"
    _run_git(git_binary, ("clone", "--bare", f"file://{fixture_repo}", str(bare_repo)), base_environment, tmp_path)
    _run_git(git_binary, ("-C", str(bare_repo), "update-server-info"), base_environment, tmp_path)

    certificate_path, private_key_path = _generate_self_signed_localhost_certificate(tmp_path / "tls")
    https_environment = {**base_environment, "GIT_SSL_CAINFO": str(certificate_path)}
    https_clone = tmp_path / "https-clone"
    with _serve_directory_over_https(serve_root, certificate_path, private_key_path) as https_base_url:
        https_clone_result = _run_git(
            git_binary,
            ("clone", f"{https_base_url}/fixture.git", str(https_clone)),
            https_environment,
            tmp_path,
        )
    assert "templates not found" not in https_clone_result.stderr, (
        f"git warned about missing templates during the https clone:\n{https_clone_result.stderr}"
    )
    https_head = _run_git(git_binary, ("rev-parse", "HEAD"), base_environment, https_clone).stdout.strip()
    assert https_head == fixture_head, (
        f"HTTPS clone HEAD {https_head} does not match the fixture HEAD {fixture_head}; "
        "the https transport did not deliver the fixture history"
    )

    # Phase 6: the payload the app actually ships has its symlinks replaced by
    # sh shims (scripts/download-binaries.js does this at download time, so
    # ToDesktop's symlink-following app-source zip cannot materialize 142
    # copies of the git binary). Convert this extracted payload with the real
    # production function, then prove the shimmed payload end to end: no
    # symlinks remain, an HTTPS clone still dispatches through the
    # git-remote-https shim, and the dashed builtin form still dispatches
    # through a libexec/git-core shim.
    shim_count = _convert_payload_symlinks_to_shims(git_root)
    assert shim_count > 0, "expected the dugite-native payload to contain symlinks to convert"
    remaining_symlinks = [entry for entry in git_root.rglob("*") if entry.is_symlink()]
    assert remaining_symlinks == [], f"symlinks remain in the payload after shim conversion: {remaining_symlinks}"
    remote_https_helper = git_root / "libexec" / "git-core" / "git-remote-https"
    assert remote_https_helper.read_bytes().startswith(b"#!/bin/sh"), (
        "git-remote-https should be an sh shim after conversion"
    )

    shimmed_https_clone = tmp_path / "https-clone-shimmed"
    with _serve_directory_over_https(serve_root, certificate_path, private_key_path) as https_base_url:
        _run_git(
            git_binary,
            ("clone", f"{https_base_url}/fixture.git", str(shimmed_https_clone)),
            https_environment,
            tmp_path,
        )
        # Dashed-form dispatch through a shim, fetching over https for good
        # measure: libexec/git-core/git-fetch is one of the ~142 former
        # symlinks to the multicall binary.
        _run_git(
            git_root / "libexec" / "git-core" / "git-fetch",
            ("origin",),
            https_environment,
            shimmed_https_clone,
        )
    shimmed_head = _run_git(git_binary, ("rev-parse", "HEAD"), base_environment, shimmed_https_clone).stdout.strip()
    assert shimmed_head == fixture_head, (
        f"HTTPS clone through the shimmed payload produced HEAD {shimmed_head}, expected {fixture_head}"
    )
