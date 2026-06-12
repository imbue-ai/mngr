"""Tests for cloud-init user_data generation."""

import yaml
from inline_snapshot import snapshot

from imbue.mngr_vps_docker.cloud_init import _indent
from imbue.mngr_vps_docker.cloud_init import generate_cloud_init_user_data
from imbue.mngr_vps_docker.host_setup import PINNED_DOCKER_APT_VERSION
from imbue.mngr_vps_docker.host_setup import PINNED_GVISOR_RELEASE

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
runcmd:
  - |
      set -e
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y curl ca-certificates gnupg rsync inotify-tools jq
  - |
      set -e
      export DEBIAN_FRONTEND=noninteractive
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
      . /etc/os-release
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
      apt-get update
      apt-get install -y --allow-downgrades docker-ce=5:29.5.1-1~debian.12~bookworm docker-ce-cli=5:29.5.1-1~debian.12~bookworm containerd.io docker-buildx-plugin docker-compose-plugin
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
    assert "get.docker.com" not in result
    assert "download.docker.com/linux/debian" in result
    assert f"docker-ce={PINNED_DOCKER_APT_VERSION}" in result
    assert "--allow-downgrades" in result
    assert "systemctl enable docker" in result
    assert "systemctl start docker" in result


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
    # Downloads the pinned dated gVisor release and registers it with the daemon.
    assert f"gvisor/releases/release/{PINNED_GVISOR_RELEASE}" in result
    assert "runsc install" in result
    # Guarded so it is a no-op when runsc is already registered.
    assert "docker info" in result
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
