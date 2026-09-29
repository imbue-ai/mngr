"""Tests for GCE startup-script generation."""

from imbue.mngr_gcp.startup_script import generate_gce_startup_script
from imbue.mngr_vps.host_setup import MNGR_READY_MARKER_PATH
from imbue.mngr_vps.host_setup import MNGR_SSHD_DROP_IN_PATH
from imbue.mngr_vps.host_setup import PINNED_DOCKER_VERSION
from imbue.mngr_vps.host_setup import PINNED_GVISOR_RELEASE
from imbue.mngr_vps.host_setup import render_sshd_drop_in_stage_script

_SAMPLE_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nline-one\nline-two\n-----END OPENSSH PRIVATE KEY-----"
_SAMPLE_PUBLIC_KEY = "ssh-ed25519 AAAATESTKEY comment"


def test_startup_script_is_a_bash_script() -> None:
    result = generate_gce_startup_script(
        host_private_key=_SAMPLE_PRIVATE_KEY,
        host_public_key=_SAMPLE_PUBLIC_KEY,
        install_gvisor_runtime=False,
    )
    # The guest agent runs the metadata verbatim, so the shebang must lead and
    # ``set -e`` must abort a half-provisioned host before the ready marker.
    assert result.startswith("#!/bin/bash\nset -e\n")


def test_startup_script_installs_host_key_and_restarts_sshd_first() -> None:
    """The host key install + sshd restart must precede the slow host setup.

    cloud-init sets the key pre-sshd; the guest agent can't, so installing it
    first shrinks the window where the server serves a boot-generated key.
    """
    result = generate_gce_startup_script(
        host_private_key=_SAMPLE_PRIVATE_KEY,
        host_public_key=_SAMPLE_PUBLIC_KEY,
        install_gvisor_runtime=False,
    )
    assert "-----BEGIN OPENSSH PRIVATE KEY-----\nline-one" in result
    assert _SAMPLE_PUBLIC_KEY in result
    host_key_index = result.index("/etc/ssh/ssh_host_ed25519_key")
    restart_index = result.index("systemctl restart ssh")
    docker_index = result.index("download.docker.com")
    assert host_key_index < restart_index < docker_index


def test_startup_script_restarts_sshd_only_when_the_host_key_or_drop_in_changes() -> None:
    """The guest agent re-runs the script on every boot; an unconditional restart would reset the
    connection ``mngr start`` opens as soon as the (already installed) expected key is served."""
    result = generate_gce_startup_script(
        host_private_key=_SAMPLE_PRIVATE_KEY,
        host_public_key=_SAMPLE_PUBLIC_KEY,
        install_gvisor_runtime=False,
    )
    guard_index = result.index('if ! cmp -s "$MNGR_SSH_STAGE/host_key" /etc/ssh/ssh_host_ed25519_key')
    install_index = result.index('install -m 0600 "$MNGR_SSH_STAGE/host_key" /etc/ssh/ssh_host_ed25519_key')
    restart_index = result.index("systemctl restart ssh")
    guard_end_index = result.index('fi\nrm -rf "$MNGR_SSH_STAGE"')
    assert guard_index < install_index < restart_index < guard_end_index
    assert "/etc/ssh/sshd_config.d/60-mngr.conf" in result[guard_index:guard_end_index]
    # The three cmp checks reach the shell as one command joined by real line continuations.
    assert result[guard_index:guard_end_index].startswith(
        'if ! cmp -s "$MNGR_SSH_STAGE/host_key" /etc/ssh/ssh_host_ed25519_key \\\n'
        '    || ! cmp -s "$MNGR_SSH_STAGE/host_key.pub" /etc/ssh/ssh_host_ed25519_key.pub \\\n'
        '    || ! cmp -s "$MNGR_SSH_STAGE/60-mngr.conf" /etc/ssh/sshd_config.d/60-mngr.conf; then\n'
    )
    # Only the GCE-only prelude is this script's own; the shared host-setup steps
    # that follow guard their own restarts.
    prelude = result[: result.index("# Forward the provider key into root")]
    restart_line = "systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart"
    assert prelude.count(restart_line) == 1, "the prelude restarts sshd only inside the cmp guard"


def test_startup_script_disables_password_auth_and_allows_root_login() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "PasswordAuthentication no" in result
    assert "PermitRootLogin prohibit-password" in result


def test_startup_script_prelude_stages_the_same_sshd_drop_in_the_shared_step_installs() -> None:
    """Both writers stage identical content, so the shared step finds the file in place and never
    restarts sshd a second time on a boot (the guest agent re-runs this script every boot)."""
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    prelude = result[: result.index("# Forward the provider key into root")]
    assert render_sshd_drop_in_stage_script("$MNGR_SSH_STAGE/60-mngr.conf") in prelude
    assert f'"$MNGR_SSH_STAGE/60-mngr.conf" {MNGR_SSHD_DROP_IN_PATH}' in prelude
    assert "MaxSessions 100" in prelude
    assert "PerSourcePenalties no" in prelude


def test_startup_script_forwards_default_user_key_to_root() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "/root/.ssh/authorized_keys" in result
    assert "ubuntu" in result
    assert "debian" in result


def test_startup_script_omits_direct_root_key_by_default() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "printf '%s\\n'" not in result


def test_startup_script_injects_authorized_user_public_key_into_root() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA host",
        install_gvisor_runtime=False,
        authorized_user_public_key="ssh-ed25519 AAAAaccess user@laptop",
    )
    assert "'ssh-ed25519 AAAAaccess user@laptop'" in result
    assert ">> /root/.ssh/authorized_keys" in result


def test_startup_script_installs_pinned_docker_not_installer_script() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "download.docker.com/linux/${ID}" in result
    assert PINNED_DOCKER_VERSION in result
    assert 'docker-ce="${DOCKER_APT_VERSION}"' in result
    assert "get.docker.com" not in result


def test_startup_script_wraps_steps_in_subshells_to_isolate_exit() -> None:
    """Steps run in subshells so a step's early ``exit`` can't skip the ready marker.

    Each host-setup step is wrapped in a subshell, so even if a step exits early
    the script still reaches the ready marker that the outer poller waits on.
    """
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=True,
    )
    assert f"gvisor/releases/release/{PINNED_GVISOR_RELEASE}" in result
    # Steps are wrapped in subshells, and the ready marker follows them.
    subshell_open_index = result.index("(\n")
    marker_index = result.index(f"touch {MNGR_READY_MARKER_PATH}")
    assert subshell_open_index < marker_index


def test_startup_script_omits_gvisor_by_default() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "runsc" not in result
    assert "gvisor" not in result


def test_startup_script_writes_ready_marker_last_after_host_setup() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    docker_index = result.index("systemctl start docker")
    marker_index = result.index(f"touch {MNGR_READY_MARKER_PATH}")
    assert docker_index < marker_index


def test_startup_script_no_shutdown_by_default() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "shutdown -P" not in result


def test_startup_script_schedules_shutdown_when_requested() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_seconds=42 * 60,
    )
    assert "shutdown -P +42" in result


def test_startup_script_rounds_sub_minute_shutdown_up() -> None:
    result = generate_gce_startup_script(
        host_private_key="k",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_seconds=90,
    )
    assert "shutdown -P +2" in result
