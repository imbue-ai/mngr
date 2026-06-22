"""Tests for cloud-init user_data generation."""

import yaml
from inline_snapshot import snapshot

from imbue.mngr_vps.cloud_init import _indent
from imbue.mngr_vps.cloud_init import generate_cloud_init_user_data
from imbue.mngr_vps.host_setup import PINNED_DOCKER_VERSION
from imbue.mngr_vps.host_setup import PINNED_GVISOR_RELEASE

# A realistic multi-line private key so the snapshot/YAML-parse tests exercise
# ``_indent`` and any future regression in the (load-bearing) indentation of the
# embedded key flips the snapshot below.
_SAMPLE_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nline-one\nline-two\n-----END OPENSSH PRIVATE KEY-----"
_SAMPLE_PUBLIC_KEY = "ssh-ed25519 AAAATESTKEY comment"


def test_indent_single_line() -> None:
    result = _indent("hello", 4)
    assert result == "    hello"


def test_indent_multiple_lines() -> None:
    result = _indent("line1\nline2\nline3", 2)
    assert result == "  line1\n  line2\n  line3"


def test_indent_zero_spaces() -> None:
    result = _indent("hello", 0)
    assert result == "hello"


def test_indent_empty_string() -> None:
    result = _indent("", 4)
    # Empty string has no lines, so splitlines returns [] and join returns ""
    assert result == ""


def test_generate_cloud_init_full_user_data_snapshot() -> None:
    # Full-document snapshot: cloud-init user_data is structured YAML where
    # indentation and key placement are load-bearing, so a substring check
    # cannot tell "the string is present" from "the document is correct". The
    # snapshot pins the exact rendered output; any structural regression (wrong
    # nesting of the private key, reordered/duplicated keys, a flipped flag)
    # changes it. Update intentionally via ``--inline-snapshot=fix``.
    result = generate_cloud_init_user_data(
        host_private_key=_SAMPLE_PRIVATE_KEY,
        host_public_key=_SAMPLE_PUBLIC_KEY,
        install_gvisor_runtime=False,
    )
    assert result == snapshot("""\
#cloud-config
ssh_deletekeys: true
ssh_keys:
  ed25519_private: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    line-one
    line-two
    -----END OPENSSH PRIVATE KEY-----
  ed25519_public: ssh-ed25519 AAAATESTKEY comment
ssh_pwauth: false
# Cloud-init disables root SSH by default (``disable_root: true``), which
# prefixes root's authorized_keys with a ``no-port-forwarding,no-X11-forwarding,
# no-agent-forwarding,no-pty,command="echo 'Please login as the user...'"``
# wrapper. mngr_vps SSHes in as root and runs interactive shell-y
# commands via pyinfra, so that wrapper would silently break every poll.
# Set to false so root's authorized_keys takes the keys verbatim.
disable_root: false
runcmd:
  # Some cloud images install the provider-side SSH key into the default
  # user's authorized_keys (e.g. AWS Debian AMIs use 'admin', AL2/AL2023
  # use 'ec2-user', Ubuntu uses 'ubuntu') rather than root's. mngr_vps
  # SSHes in as root (see ``_make_outer_for_vps_ip``), so without this
  # copy the provisioning poll loop would hang trying to authenticate.
  # Vultr / OVH put the key on root directly so this is a no-op there.
  # Paired with ``disable_root: false`` above so cloud-init doesn't prefix
  # root's keys with a ``no-pty,command="echo 'Please login as ...'"``
  # wrapper that would silently break every poll command. Runs before the
  # shared host-setup steps so root SSH becomes reachable while the long
  # apt/Docker installs are still in flight.
  - mkdir -p /root/.ssh && chmod 0700 /root/.ssh
  - for u in admin ec2-user ubuntu debian fedora centos; do if [ -f "/home/$u/.ssh/authorized_keys" ]; then cat "/home/$u/.ssh/authorized_keys" >> /root/.ssh/authorized_keys; fi; done
  - touch /root/.ssh/authorized_keys && chmod 0600 /root/.ssh/authorized_keys
  - |
      set -e
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y curl ca-certificates gnupg rsync inotify-tools jq
  - |
      set -e
      export DEBIAN_FRONTEND=noninteractive
      . /etc/os-release
      DOCKER_APT_VERSION="5:29.5.1-1~${ID}.${VERSION_ID}~${VERSION_CODENAME}"
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/${ID}/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
      apt-get update
      apt-get install -y --allow-downgrades docker-ce="${DOCKER_APT_VERSION}" docker-ce-cli="${DOCKER_APT_VERSION}" containerd.io docker-buildx-plugin docker-compose-plugin
      systemctl enable docker
      systemctl start docker
  - |
      set -e
      if ! grep -q '^MaxSessions' /etc/ssh/sshd_config 2>/dev/null; then
          printf '\\nMaxSessions 100\\nMaxStartups 100:30:200\\n' >> /etc/ssh/sshd_config
          systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true
      fi
  - touch /var/run/mngr-ready
""")


def test_generate_cloud_init_contains_host_key() -> None:
    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest-key-content\n-----END OPENSSH PRIVATE KEY-----"
    public_key = "ssh-ed25519 AAAA testkey"

    result = generate_cloud_init_user_data(
        host_private_key=private_key,
        host_public_key=public_key,
        install_gvisor_runtime=False,
    )

    assert "test-key-content" in result
    assert public_key in result


def test_generate_cloud_init_disables_password_auth() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "ssh_pwauth: false" in result


def test_generate_cloud_init_installs_pinned_docker() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    # Pinned install via the official Docker apt repo (not the unpinned get.docker.com script).
    # Repo + apt version are derived per-distro from /etc/os-release at run time.
    assert "download.docker.com/linux/${ID}" in result
    assert PINNED_DOCKER_VERSION in result
    assert 'docker-ce="${DOCKER_APT_VERSION}"' in result
    assert "--allow-downgrades" in result
    assert "systemctl enable docker" in result
    assert "systemctl start docker" in result
    # The slow installer-script approach must NOT come back -- it was the
    # root cause of the EC2 lifecycle test hitting the 300s subprocess
    # timeout on the ``mngr create`` flow.
    assert "get.docker.com" not in result


def test_generate_cloud_init_forwards_ssh_key_to_root() -> None:
    """Regression: AMIs whose cloud image installs the provider SSH key on the
    default user (admin / ec2-user / ubuntu / etc.) instead of root would make
    mngr's root-targeted SSH hang (we connect as root per ``ssh_user="root"``
    in ``mngr_vps.instance._make_outer_for_vps_ip``). cloud-init's
    runcmd copies the default user's authorized_keys into ``/root/.ssh``
    before mngr's provisioning poll loop runs, so root SSH always works.

    Vultr / OVH already install the key on root directly so this is a no-op
    there -- but emitting the shell on every provider is cheaper than
    branching in Python by provider.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "/root/.ssh/authorized_keys" in result
    assert "admin" in result
    assert "ec2-user" in result
    assert "ubuntu" in result


def test_generate_cloud_init_omits_direct_root_key_by_default() -> None:
    """Without ``authorized_user_public_key`` the direct-root-inject line is absent."""
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "printf '%s\\n'" not in result


def test_generate_cloud_init_injects_authorized_user_public_key_into_root() -> None:
    """With ``authorized_user_public_key`` set, the key is written straight into root.

    Removes the dependency on a cloud image's default-user copy landing in root
    -- notably on GCE, where the guest agent provisions the key asynchronously
    and races the runcmd copy. The key is shell-quoted so its embedded space /
    comment survive.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA host",
        install_gvisor_runtime=False,
        authorized_user_public_key="ssh-ed25519 AAAAaccess user@laptop",
    )
    assert "'ssh-ed25519 AAAAaccess user@laptop'" in result
    assert ">> /root/.ssh/authorized_keys" in result


def test_generate_cloud_init_disables_root_lockout() -> None:
    """Cloud-init defaults to ``disable_root: true``, which prefixes root's
    authorized_keys with a ``no-port-forwarding,no-X11-forwarding,no-agent-
    forwarding,no-pty,command="echo 'Please login as the user ...'"``
    wrapper. mngr_vps SSHes in as root and runs shell-y pyinfra
    commands via that account, so the wrapper would silently break every
    poll. The generated cloud-init must set ``disable_root: false`` so the
    forwarded key lands without the wrapper.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "disable_root: false" in result


def test_generate_cloud_init_creates_ready_marker() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "touch /var/run/mngr-ready" in result


def test_generate_cloud_init_deletes_existing_keys() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "ssh_deletekeys: true" in result


def test_generate_cloud_init_no_shutdown_by_default() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "shutdown -P" not in result


def test_generate_cloud_init_with_auto_shutdown_adds_shutdown_command() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_seconds=42 * 60,
    )
    assert "shutdown -P +42" in result


def test_generate_cloud_init_rounds_sub_minute_shutdown_up_to_whole_minutes() -> None:
    """`shutdown -P` takes whole minutes, so seconds round up (and never to 0)."""
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_seconds=90,
    )
    assert "shutdown -P +2" in result


def test_generate_cloud_init_with_auto_shutdown_appears_in_runcmd() -> None:
    """The shutdown entry must be inside the runcmd block, not loose YAML."""
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_seconds=15 * 60,
    )
    runcmd_index = result.index("runcmd:")
    shutdown_index = result.index("shutdown -P +15")
    assert runcmd_index < shutdown_index
    # The shutdown line is a list item under runcmd (starts with "  - ").
    line = next(line for line in result.splitlines() if "shutdown -P" in line)
    assert line.lstrip().startswith("- shutdown -P")


def test_generate_cloud_init_omits_gvisor_install_by_default() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "runsc" not in result
    assert "gvisor" not in result


def test_generate_cloud_init_includes_gvisor_install_when_requested() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=True,
    )
    # Downloads the pinned dated gVisor release and registers it with the daemon,
    # with --overlay2=none so the writable layer persists across container restart.
    assert f"gvisor/releases/release/{PINNED_GVISOR_RELEASE}" in result
    assert "runsc install -- --overlay2=none" in result
    # gnupg is installed with the base packages (needed for the Docker apt key).
    assert "gnupg" in result


def test_generate_cloud_init_parses_as_yaml_with_key_at_correct_nesting() -> None:
    # Beyond the textual snapshot, prove the output is well-formed YAML and that
    # the private key lands under ``ssh_keys.ed25519_private`` with its content
    # intact -- a wrong-indentation regression would either fail to parse here
    # or surface the key at the wrong nesting level, which a substring check
    # could never catch. (cloud-init user_data is YAML by definition; this test
    # parses the format we are forced to emit, it does not introduce new YAML.)
    result = generate_cloud_init_user_data(
        host_private_key=_SAMPLE_PRIVATE_KEY,
        host_public_key=_SAMPLE_PUBLIC_KEY,
        install_gvisor_runtime=False,
    )
    assert result.startswith("#cloud-config\n")

    parsed = yaml.safe_load(result)
    # The block scalar preserves the key content (with a trailing newline).
    assert parsed["ssh_keys"]["ed25519_private"].strip() == _SAMPLE_PRIVATE_KEY
    assert parsed["ssh_keys"]["ed25519_public"] == _SAMPLE_PUBLIC_KEY
    assert parsed["ssh_pwauth"] is False
    assert parsed["ssh_deletekeys"] is True
    # The provisioning commands are emitted as a list of runcmd shell scripts;
    # the Docker install and ready-marker steps must survive the YAML round-trip.
    runcmd_joined = "\n".join(parsed["runcmd"])
    assert "systemctl enable docker" in runcmd_joined
    assert "systemctl start docker" in runcmd_joined
    assert "touch /var/run/mngr-ready" in runcmd_joined
