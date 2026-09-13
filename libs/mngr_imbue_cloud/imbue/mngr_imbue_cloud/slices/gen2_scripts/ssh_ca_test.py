import subprocess

import pytest

from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import EmptySshCaPublicKeyError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_ANALYTICS_PRINCIPALS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_CONNECTOR_PRINCIPALS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_OPERATOR_PRINCIPALS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PRINCIPAL_OPERATOR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PRINCIPAL_SERVICE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import is_same_ssh_public_key
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import render_ssh_ca_trust_shell_section
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import ssh_ca_trust_files
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import ssh_ca_vault_mount

_CA = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE minds-dev-ca"


def test_trust_files_carry_the_ca_the_drop_in_and_one_principals_file_per_user() -> None:
    files = ssh_ca_trust_files(
        _CA + "\n", {"slicehost": SSH_CA_PRINCIPAL_SERVICE, "debian": SSH_CA_PRINCIPAL_OPERATOR}
    )
    content_by_path = {trust_file.path: trust_file.content for trust_file in files}
    assert content_by_path["/etc/ssh/mngr_user_ca.pub"] == _CA + "\n"
    assert content_by_path["/etc/ssh/sshd_config.d/61-mngr-user-ca.conf"] == (
        "TrustedUserCAKeys /etc/ssh/mngr_user_ca.pub\nAuthorizedPrincipalsFile /etc/ssh/principals/%u\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
    )
    assert content_by_path["/etc/ssh/principals/debian"] == "mngr-operator\n"
    assert content_by_path["/etc/ssh/principals/slicehost"] == "mngr-service\n"
    assert {trust_file.mode for trust_file in files} == {"0644"}


def test_trust_files_refuse_an_empty_ca() -> None:
    with pytest.raises(EmptySshCaPublicKeyError):
        ssh_ca_trust_files("   ", {"debian": SSH_CA_PRINCIPAL_OPERATOR})


def test_shell_section_passes_bash_syntax_check_and_writes_every_file() -> None:
    section = render_ssh_ca_trust_shell_section(_CA, {"debian": SSH_CA_PRINCIPAL_OPERATOR})
    result = subprocess.run(["bash", "-n"], input=section, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "cat > /etc/ssh/mngr_user_ca.pub <<'MNGR_SSH_CA_FILE'" in section
    assert "cat > /etc/ssh/principals/debian <<'MNGR_SSH_CA_FILE'" in section
    assert "chmod 0644 /etc/ssh/sshd_config.d/61-mngr-user-ca.conf" in section


def test_role_principal_sets_keep_the_operator_principal_out_of_service_roles() -> None:
    assert SSH_CA_PRINCIPAL_OPERATOR in SSH_CA_OPERATOR_PRINCIPALS
    assert SSH_CA_PRINCIPAL_OPERATOR not in SSH_CA_CONNECTOR_PRINCIPALS
    assert SSH_CA_PRINCIPAL_SERVICE not in SSH_CA_ANALYTICS_PRINCIPALS
    assert set(SSH_CA_ANALYTICS_PRINCIPALS) < set(SSH_CA_CONNECTOR_PRINCIPALS) < set(SSH_CA_OPERATOR_PRINCIPALS)


def test_vault_mount_is_per_tier() -> None:
    assert ssh_ca_vault_mount("production") == "minds-production-ssh"


def test_is_same_ssh_public_key_ignores_the_comment_and_nothing_else() -> None:
    served = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGxpdmUga2V5IGJ5dGVzIGhlcmUgZm9yIHRlc3Rz"
    assert is_same_ssh_public_key(served, f"{served} root@vm\n")
    assert is_same_ssh_public_key(f"{served}\n", served)
    assert not is_same_ssh_public_key(served, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG90aGVyIGtleSBieXRlcyBoZXJl")
    assert not is_same_ssh_public_key(served, "")
