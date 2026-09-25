import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from packaging.version import Version

from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import GATEWAY_MAX_BODY_SIZE_BYTES
from imbue.mngr_latchkey.core import LATCHKEY_MIN_VERSION
from imbue.mngr_latchkey.core import REMOTE_GATEWAY_EXTENSION_FILENAME
from imbue.mngr_latchkey.core import bundled_gateway_extension_content
from imbue.mngr_latchkey.docker_bridge import BRIDGE_SERVICES_FIREWALL_UNIT_NAME
from imbue.mngr_latchkey.docker_bridge import BRIDGE_SERVICES_NFT_TABLE
from imbue.mngr_latchkey.owner_exec_vm import VM_EXEC_PORT
from imbue.mngr_latchkey.remote._machine import DIFFERENT_MACHINE_PASSWORD_MESSAGE
from imbue.mngr_latchkey.remote._machine import REMOTE_COMMAND_NAME
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.remote.mock_outer_host_test import rooted_layout
from imbue.mngr_latchkey.remote.package import CURL_SHIMS_SHA256_BY_TRIPLE
from imbue.mngr_latchkey.remote.package import CURL_SHIMS_VERSION
from imbue.mngr_latchkey.remote.package import DEFAULT_REMOTE_PACKAGE_LAYOUT
from imbue.mngr_latchkey.remote.package import DESKTOP_GATEWAY_VPS_PORT
from imbue.mngr_latchkey.remote.package import GATEWAY_PROGRAM_NAME
from imbue.mngr_latchkey.remote.package import LATCHKEY_VERSION
from imbue.mngr_latchkey.remote.package import MINIMUM_NODE_MAJOR_VERSION
from imbue.mngr_latchkey.remote.package import OUTER_PORT
from imbue.mngr_latchkey.remote.package import PACKAGE_NAME
from imbue.mngr_latchkey.remote.package import RemotePackageArtifact
from imbue.mngr_latchkey.remote.package import RemotePackageLayout
from imbue.mngr_latchkey.remote.package import TUNNEL_PROGRAM_NAME
from imbue.mngr_latchkey.remote.package import _read_package_sources
from imbue.mngr_latchkey.remote.package import _render
from imbue.mngr_latchkey.remote.package import _require_complete_sources
from imbue.mngr_latchkey.remote.package import build_remote_package
from imbue.mngr_latchkey.remote.package import remote_package_context
from imbue.mngr_latchkey.remote.package import render_bootstrap_script
from imbue.mngr_latchkey.testing import extract_deb_data
from imbue.mngr_latchkey.testing import read_deb_control_field
from imbue.mngr_latchkey.testing import read_deb_members
from imbue.mngr_latchkey.testing import read_deb_tar

_SHELL_TIMEOUT_SECONDS = 30.0

# Every script the package carries, by its path in the package.
_SCRIPT_PATHS = (
    "DEBIAN/postinst",
    "DEBIAN/prerm",
    "DEBIAN/postrm",
    "usr/bin/mngr-latchkey",
    "usr/lib/mngr-latchkey/read-state",
    "usr/lib/mngr-latchkey/apply-state",
    "usr/lib/mngr-latchkey/gateway-run",
    "usr/lib/mngr-latchkey/tunnel-run",
)


def _build() -> RemotePackageArtifact:
    return build_remote_package(remote_package_context(DEFAULT_REMOTE_PACKAGE_LAYOUT))


def _unpacked(tmp_path: Path) -> tuple[RemotePackageArtifact, Path]:
    """The package built for the default layout, unpacked (control files under ``DEBIAN/``) at a directory."""
    artifact = _build()
    root = tmp_path / "unpacked"
    extract_deb_data(artifact.content, root)
    with read_deb_tar(artifact.content, "control.tar.gz") as tar:
        tar.extractall(root / "DEBIAN", filter="data")
    return artifact, root


def _code_lines(script: str) -> str:
    """The script without its comment lines, so an assertion about what it runs cannot match what it explains."""
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


def _shell_syntax_check(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True, timeout=_SHELL_TIMEOUT_SECONDS)


def test_minimum_version_is_not_newer_than_the_version_we_install() -> None:
    """A floor newer than the version we install means a half-finished bump.

    See the comment above :data:`LATCHKEY_MIN_VERSION` for why the pins move
    together.
    """
    assert Version(LATCHKEY_MIN_VERSION) <= Version(LATCHKEY_VERSION), (
        f"LATCHKEY_MIN_VERSION={LATCHKEY_MIN_VERSION} is newer than the installed "
        f"LATCHKEY_VERSION={LATCHKEY_VERSION}; raise the install pin to at least the minimum."
    )


def test_workspace_and_vps_gateway_ports_are_consistent() -> None:
    assert OUTER_PORT == AGENT_SIDE_LATCHKEY_PORT
    assert DESKTOP_GATEWAY_VPS_PORT != OUTER_PORT


# The archive.


def test_the_build_is_reproducible_and_versioned_by_its_inputs() -> None:
    """Two builds of the same sources and context are the same bytes, under the same version."""
    first = _build()
    second = _build()

    assert first.content == second.content
    assert first.version == second.version
    assert first.filename == f"{PACKAGE_NAME}_{first.version}_all.deb"
    assert re.fullmatch(r"\d+\.\d+\.\d+\+[0-9a-f]{12}", first.version), first.version


def test_a_different_layout_is_a_different_version(tmp_path: Path) -> None:
    """The version hashes the context too, so a package built for another layout never passes as this one."""
    default = _build()
    rooted = build_remote_package(remote_package_context(rooted_layout(tmp_path)))

    assert rooted.version != default.version


def test_the_archive_is_a_debian_binary_package() -> None:
    artifact = _build()

    members = read_deb_members(artifact.content)

    # dpkg requires exactly this order: the format stamp, then control, then data.
    assert list(members) == ["debian-binary", "control.tar.gz", "data.tar.gz"]
    assert members["debian-binary"] == b"2.0\n"
    assert read_deb_control_field(artifact.content, "Package") == PACKAGE_NAME
    assert read_deb_control_field(artifact.content, "Version") == artifact.version
    assert read_deb_control_field(artifact.content, "Architecture") == "all"
    assert (
        read_deb_control_field(artifact.content, "Depends")
        == "supervisor, curl, ca-certificates, openssh-client, nftables"
    )


def test_the_package_lays_its_files_out_where_the_scripts_expect_them() -> None:
    artifact = _build()

    with read_deb_tar(artifact.content, "data.tar.gz") as tar:
        entries = {entry.name: entry for entry in tar.getmembers()}

    assert {name for name in entries if entries[name].isfile()} == {
        "./etc/nftables.d/mngr-bridge-services.nft",
        "./etc/supervisor/conf.d/latchkey-gateway.conf",
        "./etc/supervisor/conf.d/latchkey-tunnel.conf",
        "./etc/systemd/system/mngr-bridge-services-firewall.service",
        "./usr/bin/mngr-latchkey",
        "./usr/lib/mngr-latchkey/apply-state",
        "./usr/lib/mngr-latchkey/extensions/desktop_gateway_proxy.mjs",
        "./usr/lib/mngr-latchkey/functions",
        "./usr/lib/mngr-latchkey/gateway-run",
        "./usr/lib/mngr-latchkey/read-state",
        "./usr/lib/mngr-latchkey/tunnel-run",
    }
    # The log directory ships empty for supervisord to write into, listed with
    # its parents like every other directory the package creates.
    assert entries["./var/log/mngr-latchkey"].isdir()
    assert entries["./var/log"].isdir() and entries["./var"].isdir()
    # Scripts are executable, and everything else (including the sourced
    # functions file and the extension) is not.
    assert stat.S_IMODE(entries["./usr/bin/mngr-latchkey"].mode) == 0o755
    assert stat.S_IMODE(entries["./usr/lib/mngr-latchkey/apply-state"].mode) == 0o755
    assert stat.S_IMODE(entries["./usr/lib/mngr-latchkey/functions"].mode) == 0o644
    assert stat.S_IMODE(entries["./usr/lib/mngr-latchkey/extensions/desktop_gateway_proxy.mjs"].mode) == 0o644
    assert all(entry.uname == "root" and entry.gname == "root" for entry in entries.values())


def test_the_package_carries_the_bundled_forwarding_extension(tmp_path: Path) -> None:
    """One copy of the extension in the repo: the package takes the one the desktop code bundles."""
    _, root = _unpacked(tmp_path)

    shipped = (root / "usr/lib/mngr-latchkey/extensions" / REMOTE_GATEWAY_EXTENSION_FILENAME).read_text()

    assert shipped == bundled_gateway_extension_content(REMOTE_GATEWAY_EXTENSION_FILENAME)
    assert "Desktop latchkey gateway is unreachable" in shipped


def test_every_script_is_well_formed_posix_shell(tmp_path: Path) -> None:
    artifact, root = _unpacked(tmp_path)
    # The bootstrap lives outside the tree and is the one script the fake VPS
    # never runs (it acts the install out instead), so it is rendered here.
    bootstrap = tmp_path / "bootstrap.sh"
    bootstrap.write_text(render_bootstrap_script(remote_package_context(DEFAULT_REMOTE_PACKAGE_LAYOUT), artifact))
    # The sourced functions file has no shebang (it is never run) but must parse too.
    sourced_or_rendered = (root / "usr/lib/mngr-latchkey/functions", bootstrap)

    for script in _SCRIPT_PATHS:
        assert (root / script).read_text().startswith("#!/bin/sh\n"), script
    for script_path in (*(root / script for script in _SCRIPT_PATHS), *sourced_or_rendered):
        checked = _shell_syntax_check(script_path)
        assert checked.returncode == 0, f"{script_path}: {checked.stderr}"


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb is not installed here")
def test_dpkg_accepts_the_package(tmp_path: Path) -> None:
    """The real tool reads what the pure-Python build wrote."""
    artifact = _build()
    deb_path = tmp_path / artifact.filename
    deb_path.write_bytes(artifact.content)

    info = subprocess.run(
        ["dpkg-deb", "--info", str(deb_path)], capture_output=True, text=True, timeout=_SHELL_TIMEOUT_SECONDS
    )
    contents = subprocess.run(
        ["dpkg-deb", "--contents", str(deb_path)], capture_output=True, text=True, timeout=_SHELL_TIMEOUT_SECONDS
    )

    assert info.returncode == 0, info.stderr
    assert f"Version: {artifact.version}" in info.stdout
    assert "postinst" in info.stdout and "prerm" in info.stdout and "postrm" in info.stdout
    assert contents.returncode == 0, contents.stderr
    assert "./usr/lib/mngr-latchkey/apply-state" in contents.stdout


def test_sources_that_lost_the_script_tree_are_refused() -> None:
    """A build from sources missing part of the tree fails here, not as a missing file on the machine."""
    sources = {path: content for path, content in _read_package_sources().items() if "/usr/lib/" not in path}

    with pytest.raises(RemoteGatewayError, match="lack tree/usr/lib/mngr-latchkey/"):
        _require_complete_sources(sources)


def test_a_template_naming_something_the_context_lacks_fails_to_render() -> None:
    """Rendering is strict: a typo in a template can never ship as an empty string on a machine."""
    with pytest.raises(RemoteGatewayError, match="Failed to render"):
        _render("broken.j2", b"exec {{ no_such_variable }}\n", {"package_name": PACKAGE_NAME})


# What the scripts say.


@pytest.mark.parametrize(
    ("directory_name", "elsewhere", "path_in_tree"),
    [
        ("install_dir", "/opt/mngr-latchkey", "usr/lib/mngr-latchkey"),
        ("bin_dir", "/usr/local/bin", "usr/bin"),
        ("log_dir", "/var/log/latchkey", "var/log/mngr-latchkey"),
        ("supervisor_confd_dir", "/etc/supervisord.d", "etc/supervisor/conf.d"),
    ],
)
def test_a_layout_that_disagrees_with_the_package_tree_is_refused(
    directory_name: str, elsewhere: str, path_in_tree: str
) -> None:
    """The scripts name the layout's directories while dpkg unpacks the files at the tree's, so they must agree."""
    with pytest.raises(RemoteGatewayError, match=f"{directory_name} must be {path_in_tree}"):
        RemotePackageLayout.model_validate(
            {**DEFAULT_REMOTE_PACKAGE_LAYOUT.model_dump(), directory_name: Path(elsewhere)}
        )


def test_the_context_places_the_curl_shims_and_the_firewall_policy_under_the_layout(tmp_path: Path) -> None:
    layout = rooted_layout(tmp_path)

    context = remote_package_context(layout)

    assert context.curl_router_path == layout.curl_install_dir / "latchkey-curl-router"
    assert context.curl_impersonate_path == layout.curl_install_dir / "curl-impersonate"
    assert context.curl_version_stamp_path == layout.curl_install_dir / ".latchkey-curl-version"
    assert context.nft_policy_path == layout.nftables_confd_dir / "mngr-bridge-services.nft"


def test_postinst_installs_the_pinned_latchkey_and_bounces_the_gateway_on_an_upgrade(tmp_path: Path) -> None:
    _, root = _unpacked(tmp_path)

    postinst = _code_lines((root / "DEBIAN/postinst").read_text())

    assert f"npm install -g 'latchkey@{LATCHKEY_VERSION}'" in postinst
    # The reinstall is gated on the installed package.json's version, never on running the CLI.
    assert f"""2>/dev/null)" != '{LATCHKEY_VERSION}' ]""" in postinst
    assert "latchkey --version" not in postinst
    assert '"$(npm root -g)/latchkey/package.json"' in postinst
    # A supervisord-managed gateway keeps the old code in memory across an npm
    # upgrade, so the install branch bounces it -- after the programs are
    # (re)registered, tolerating a host where the program was not there yet.
    assert postinst.index("npm install -g") < postinst.index("supervisorctl update")
    assert postinst.index("supervisorctl update") < postinst.index(
        'supervisorctl restart "$LK_GATEWAY_PROGRAM" || true'
    )
    # No apt here: dpkg holds its lock while postinst runs.
    assert "apt-get" not in postinst
    # A stale node shadowing the bootstrap's install fails with an actionable message.
    assert f'[ "$_lk_node_major" -lt {MINIMUM_NODE_MAJOR_VERSION} ]' in postinst
    assert "shadowing installation" in postinst


def test_postinst_installs_the_curl_shims_version_gated(tmp_path: Path) -> None:
    _, root = _unpacked(tmp_path)

    postinst = (root / "DEBIAN/postinst").read_text()

    # Fetches the latchkey-curl-shims tarball for the VPS arch from the pinned
    # release, verifies it against the sha256 pinned at build time (never one
    # downloaded beside it), and installs both the router and the impersonator
    # it fronts.
    assert f"github.com/imbue-ai/latchkey-curl-shims/releases/download/{CURL_SHIMS_VERSION}/" in postinst
    assert '_lk_tarball="latchkey-curl-shims-${_lk_triple}.tar.gz"' in postinst
    # Every arch the script resolves lands on a statically linked musl build,
    # each with its own pinned sum.
    assert dict(re.findall(r"_lk_triple=(\S+); _lk_sha256=(\S+) ;;", postinst)) == dict(CURL_SHIMS_SHA256_BY_TRIPLE)
    assert 'echo "${_lk_sha256}  ${_lk_tarball}" | sha256sum -c -' in postinst
    assert ".sha256" not in postinst
    assert "tar -xzf" in postinst and "--strip-components=1" in postinst
    # The download scratch is dropped however the install ends: a pass that
    # fails part-way is retried every cycle and must not fill /tmp.
    assert "trap 'rm -rf \"$_lk_tmp\"' EXIT" in postinst
    router = "/usr/local/bin/latchkey-curl-router"
    impersonate = "/usr/local/bin/curl-impersonate"
    # Both binaries are staged beside their destinations and then renamed into
    # place (overwriting a running binary fails with ETXTBSY); the router --
    # the one LATCHKEY_CURL names -- is swapped last, and the version stamp is
    # written only once both are in place.
    assert f"install -m 0755 \"${{_lk_tmp}}/latchkey-curl-router\" '{router}.new'" in postinst
    assert f"install -m 0755 \"${{_lk_tmp}}/curl-impersonate\" '{impersonate}.new'" in postinst
    assert postinst.index(f"mv -f '{impersonate}.new'") < postinst.index(f"mv -f '{router}.new'")
    assert postinst.index(f"mv -f '{router}.new'") < postinst.index("> '/usr/local/bin/.latchkey-curl-version'")
    # Version-gated, not presence-gated: the binaries live under version-less
    # names, so a VPS with an older release's pair is re-installed.
    assert f"[ ! -x '{router}' ] || " in postinst
    assert """[ "$(cat '/usr/local/bin/.latchkey-curl-version' 2>/dev/null)" != "$_lk_curl_want" ]""" in postinst
    assert f'_lk_curl_want="{CURL_SHIMS_VERSION} ${{_lk_triple}}"' in postinst


def test_the_package_fences_the_bridge_bound_ports_onto_the_docker_bridge(tmp_path: Path) -> None:
    """The nftables policy and the oneshot that loads it at boot ship in the package; postinst loads it first."""
    _, root = _unpacked(tmp_path)

    policy = (root / "etc/nftables.d/mngr-bridge-services.nft").read_text()
    unit = (root / f"etc/systemd/system/{BRIDGE_SERVICES_FIREWALL_UNIT_NAME}.service").read_text()
    postinst = _code_lines((root / "DEBIAN/postinst").read_text())
    postrm = _code_lines((root / "DEBIAN/postrm").read_text())

    # Its own table, added-deleted-added so re-loading converges; the two
    # bridge-bound ports are dropped off every interface but the bridge and
    # loopback, and the bridge may open new connections to those ports only.
    assert policy.startswith("#!/usr/sbin/nft -f\n")
    assert f"add table inet {BRIDGE_SERVICES_NFT_TABLE}\ndelete table inet {BRIDGE_SERVICES_NFT_TABLE}\n" in policy
    assert f'iifname != "docker0" iifname != "lo" tcp dport {{ {OUTER_PORT}, {VM_EXEC_PORT} }} counter drop' in policy
    assert f'iifname "docker0" tcp dport {{ {OUTER_PORT}, {VM_EXEC_PORT} }} accept' in policy
    assert 'iifname "docker0" ct state new counter drop' in policy
    assert "type filter hook input priority filter; policy accept;" in policy
    # Loaded at boot before the services it protects, after a distro policy
    # that would flush it.
    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit
    assert "After=nftables.service" in unit
    assert "Before=docker.service owner-exec-vm.service supervisor.service" in unit
    assert "ExecStart=/usr/sbin/nft -f /etc/nftables.d/mngr-bridge-services.nft" in unit
    assert "WantedBy=multi-user.target" in unit
    # Loaded now by the install too, ahead of the programs it protects; a
    # policy that cannot be loaded fails the install under its "set -e".
    assert "systemctl daemon-reload\n" in postinst
    assert f"systemctl enable --now '{BRIDGE_SERVICES_FIREWALL_UNIT_NAME}'\n" in postinst
    assert f"systemctl restart '{BRIDGE_SERVICES_FIREWALL_UNIT_NAME}'\n" in postinst
    assert postinst.index("systemctl restart") < postinst.index("supervisorctl reread")
    assert "|| true" not in postinst.splitlines()[postinst.splitlines().index("systemctl daemon-reload")]
    # Removal disables the boot-time load; a purge unloads the table too.
    assert f"systemctl disable '{BRIDGE_SERVICES_FIREWALL_UNIT_NAME}'" in postrm
    assert f"/usr/sbin/nft delete table inet '{BRIDGE_SERVICES_NFT_TABLE}'" in postrm


def test_postinst_scrubs_what_the_ad_hoc_provisioning_left_behind(tmp_path: Path) -> None:
    _, root = _unpacked(tmp_path)

    postinst = _code_lines((root / "DEBIAN/postinst").read_text())

    # The wrapper the ad-hoc provisioning wrote beside the store, the extension
    # copy it staged, and secrets an older build kept on disk go; nothing is
    # killed by PID any more (no pre-supervisord machine remains).
    assert '"$LK_LATCHKEY_DIR/gateway_encryption_key"' in postinst
    assert '"$LK_LATCHKEY_DIR/gateway_run.sh"' in postinst
    assert '"$LK_EXTENSIONS_DIR/desktop_gateway_proxy.mjs.candidate"' in postinst
    assert "kill" not in postinst
    assert ".pid" not in postinst
    assert postinst.index("supervisorctl reread") < postinst.index("supervisorctl update")


def test_apply_state_refreshes_the_gateway_extension_from_the_packages_copy(tmp_path: Path) -> None:
    """A copy, never a symlink: the gateway's Dirent.isFile() filter skips a linked extension without a word."""
    _, root = _unpacked(tmp_path)

    apply_state = _code_lines((root / "usr/lib/mngr-latchkey/apply-state").read_text())

    assert 'lk_install_file "$LK_EXTENSIONS_DIR/desktop_gateway_proxy.mjs" "$LK_PACKAGE_EXTENSION"' in apply_state
    assert "ln -s" not in apply_state
    # Unconditional (no document entry gates it), and in place before the restart that loads it.
    assert apply_state.index("if lk_doc_has tunnel_host_id") < apply_state.index('mkdir -p "$LK_EXTENSIONS_DIR"')
    assert apply_state.index('mkdir -p "$LK_EXTENSIONS_DIR"') < apply_state.index("if lk_doc_has restart_gateway")


def test_apply_state_installs_the_desktop_egress_rules_ahead_of_the_policy(tmp_path: Path) -> None:
    """The policy stays the last thing an apply installs."""
    _, root = _unpacked(tmp_path)

    apply_state = _code_lines((root / "usr/lib/mngr-latchkey/apply-state").read_text())

    rules_install = 'lk_install_file "$LK_DESKTOP_EGRESS_RULES_FILE" "$(lk_doc_file desktop_egress_rules_json)"'
    policy_install = 'lk_install_file "$LK_PERMISSIONS_FILE" "$(lk_doc_file permissions_json)"'
    assert apply_state.index(rules_install) < apply_state.index(policy_install)


def test_the_gateway_wrapper_reads_its_secrets_from_files_and_execs_the_gateway(tmp_path: Path) -> None:
    _, root = _unpacked(tmp_path)

    run_script = (root / "usr/lib/mngr-latchkey/gateway-run").read_text()
    functions = (root / "usr/lib/mngr-latchkey/functions").read_text()

    # The listen port is named the way upstream reads it, so OUTER_PORT is what
    # the gateway actually binds rather than latchkey's default happening to match.
    assert f"export LATCHKEY_GATEWAY_LISTEN_PORT={OUTER_PORT}" in run_script
    # The gateway binds the address apply-state wrote (the docker bridge
    # address, reachable from the agent's container and never from off-host),
    # never a loopback or wildcard spelled into the script.
    assert "lk_source_gateway_conf" in run_script
    assert 'export LATCHKEY_GATEWAY_LISTEN_HOST="$LK_GATEWAY_LISTEN_HOST"' in run_script
    assert "LISTEN_HOST=127.0.0.1" not in run_script
    assert "LISTEN_HOST=0.0.0.0" not in run_script
    assert 'LK_GATEWAY_CONF="$LK_LATCHKEY_DIR/gateway.conf"' in functions
    assert "awaiting provisioning" in functions
    assert "export LATCHKEY_DISABLE_COUNTING=1" in run_script
    # The machine renews its own tokens: the store it runs on is its own.
    assert "LATCHKEY_DISABLE_CREDENTIALS_REFRESH" not in run_script
    assert f"export LATCHKEY_EXTENSION_DESKTOP_GATEWAY_URL='http://127.0.0.1:{DESKTOP_GATEWAY_VPS_PORT}'" in run_script
    # exec so supervisord tracks the gateway PID directly, with the same
    # body-size limit the desktop-side gateway uses.
    assert f"exec latchkey gateway --max-body-size {GATEWAY_MAX_BODY_SIZE_BYTES}" in run_script
    # The machine's own secrets are read from their 0600 files into the
    # environment; the desktop-owned pair is handed over as file *paths*, read
    # per request, so another computer's pass takes effect without a restart.
    assert 'LATCHKEY_ENCRYPTION_KEY="$(lk_secret_value "$LK_ENCRYPTION_KEY_FILE")"' in run_script
    assert 'LATCHKEY_GATEWAY_LISTEN_PASSWORD="$(lk_secret_value "$LK_LISTEN_PASSWORD_FILE")"' in run_script
    assert 'export LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PASSWORD_FILE="$LK_DESKTOP_PASSWORD_FILE"' in run_script
    assert 'LK_DESKTOP_PASSWORD_FILE="$LK_SECRETS_DIR/desktop_gateway_password"' in functions
    assert 'LK_DESKTOP_OVERRIDE_FILE="$LK_SECRETS_DIR/desktop_permissions_override"' in functions
    assert "LK_SECRETS_DIR='/run/mngr-latchkey'" in functions
    # Routes latchkey through the curl router, unconditionally.
    assert "export LATCHKEY_CURL='/usr/local/bin/latchkey-curl-router'" in run_script
    # The router fails every request when the rules file it is pointed at is
    # missing, so the wrapper creates an empty one before exporting the path,
    # and never overwrites rules a machine already has.
    assert 'LK_DESKTOP_EGRESS_RULES_FILE="$LK_LATCHKEY_DIR/proxyRules.json"' in functions
    assert (
        'if [ ! -f "$LK_DESKTOP_EGRESS_RULES_FILE" ]; then\n'
        "  (umask 077 && printf '{}\\n' > \"$LK_DESKTOP_EGRESS_RULES_FILE\")\n"
        "fi"
    ) in run_script
    assert (
        run_script.index('> "$LK_DESKTOP_EGRESS_RULES_FILE"')
        < run_script.index('export LATCHKEY_DESKTOP_PROXY_CONFIG="$LK_DESKTOP_EGRESS_RULES_FILE"')
        < run_script.index("exec latchkey gateway")
    )
    # The router looks the rules up by the service latchkey names in this
    # header, and latchkey sends the header only when this variable asks for it.
    assert "export LATCHKEY_DIAGNOSTIC_HEADERS=1\n" in run_script
    # Refuses to launch a keyless gateway when the RAM-backed secrets are gone;
    # the desktop-owned pair is deliberately not part of that gate.
    assert "awaiting re-provision" in run_script
    assert 'LK_DESKTOP_OVERRIDE_FILE" ]' not in run_script


def test_the_supervisord_programs_keep_both_processes_up(tmp_path: Path) -> None:
    _, root = _unpacked(tmp_path)

    gateway = (root / "etc/supervisor/conf.d/latchkey-gateway.conf").read_text()
    tunnel = (root / "etc/supervisor/conf.d/latchkey-tunnel.conf").read_text()

    assert f"[program:{GATEWAY_PROGRAM_NAME}]" in gateway
    assert "command=/bin/sh /usr/lib/mngr-latchkey/gateway-run" in gateway
    assert f"[program:{TUNNEL_PROGRAM_NAME}]" in tunnel
    assert "command=/bin/sh /usr/lib/mngr-latchkey/tunnel-run" in tunnel
    for conf in (gateway, tunnel):
        assert "autorestart=true" in conf
        assert "stopasgroup=true" in conf
        assert "stdout_logfile=/var/log/mngr-latchkey/" in conf
        assert "stdout_logfile_maxbytes=10MB" in conf
    # The gateway comes up on its own (and goes quietly FATAL after a reboot);
    # the tunnel is started only for a container that needs it, and once
    # started keeps retrying while the container's sshd comes up.
    assert "autostart=true" in gateway
    assert "startretries=3" in gateway
    assert "autostart=false" in tunnel
    assert "startretries=1000000" in tunnel


def test_the_tunnel_wrapper_reverse_forwards_the_agent_port_with_keepalives(tmp_path: Path) -> None:
    _, root = _unpacked(tmp_path)

    tunnel_run = (root / "usr/lib/mngr-latchkey/tunnel-run").read_text()

    # The container's loopback port is forwarded to the address the gateway
    # binds (its docker bridge address, read from the same file the gateway
    # wrapper reads), not to the machine's loopback.
    assert f'-R "127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}:$LK_GATEWAY_LISTEN_HOST:{OUTER_PORT}"' in tunnel_run
    assert f":127.0.0.1:{OUTER_PORT}" not in tunnel_run
    assert "lk_source_gateway_conf" in tunnel_run
    assert "exec /usr/bin/ssh -N -T" in tunnel_run
    for option in (
        "StrictHostKeyChecking=no",
        "UserKnownHostsFile=/dev/null",
        "ExitOnForwardFailure=yes",
        "BatchMode=yes",
        "ConnectTimeout=15",
        "ServerAliveInterval=30",
        "ServerAliveCountMax=3",
    ):
        assert f"-o {option}" in tunnel_run
    # The target is read from the file apply-state writes; without one the
    # program exits with a clear message for supervisord to retry.
    assert '. "$LK_TUNNEL_CONF"' in tunnel_run
    assert '"$LK_CONTAINER_SSH_USER@127.0.0.1"' in tunnel_run
    assert "awaiting provisioning" in tunnel_run


def test_a_message_with_an_apostrophe_is_quoted_into_the_script(tmp_path: Path) -> None:
    """Every string rendered into a shell word is shell-quoted, so no message can break its script."""
    _, root = _unpacked(tmp_path)

    apply_state = (root / "usr/lib/mngr-latchkey/apply-state").read_text()

    assert "'" in DIFFERENT_MACHINE_PASSWORD_MESSAGE
    assert DIFFERENT_MACHINE_PASSWORD_MESSAGE.replace("'", "'\"'\"'") in apply_state


def test_the_command_dispatches_only_the_two_state_scripts(tmp_path: Path) -> None:
    _, root = _unpacked(tmp_path)

    command = (root / "usr/bin" / REMOTE_COMMAND_NAME).read_text()

    assert "read-state|apply-state)" in command
    assert "exec '/usr/lib/mngr-latchkey'/\"$1\"" in command


# The bootstrap.


def test_the_bootstrap_puts_the_dependencies_in_place_and_installs_the_package() -> None:
    artifact = _build()

    bootstrap = _code_lines(render_bootstrap_script(remote_package_context(DEFAULT_REMOTE_PACKAGE_LAYOUT), artifact))

    assert bootstrap.startswith("set -e\n")
    # POSIX sh compatibility: must not rely on bash-only pipefail.
    assert "pipefail" not in bootstrap
    assert f"_lk_deb='/tmp/{artifact.filename}'" in bootstrap
    # The package's Debian dependencies, installed only when missing.
    assert "for _lk_pkg in supervisor curl ca-certificates openssh-client nftables; do" in bootstrap
    assert 'dpkg -s "$_lk_pkg"' in bootstrap
    assert "apt-get update" in bootstrap and "apt-get install -y $_lk_missing" in bootstrap
    assert "systemctl enable --now supervisor" in bootstrap
    # Node.js is gated behind a *version* probe, not mere presence.
    assert "node --version" in bootstrap
    assert f'[ "$_lk_node_major" -lt {MINIMUM_NODE_MAJOR_VERSION} ]' in bootstrap
    assert "deb.nodesource.com/setup_" in bootstrap
    assert "apt-get install -y nodejs" in bootstrap
    assert 'dpkg -i "$_lk_deb"' in bootstrap
    assert 'rm -f "$_lk_deb"' in bootstrap


def test_the_bootstrap_survives_a_replay_after_the_artifact_was_consumed() -> None:
    """A retried command whose first run already installed and removed the artifact must not fail on it."""
    artifact = _build()

    bootstrap = render_bootstrap_script(remote_package_context(DEFAULT_REMOTE_PACKAGE_LAYOUT), artifact)

    assert (
        f"""if [ -f "$_lk_deb" ] || [ "$(dpkg-query -W -f '${{Version}}' '{PACKAGE_NAME}' 2>/dev/null || true)" """
        f"""!= '{artifact.version}' ]; then"""
    ) in bootstrap
