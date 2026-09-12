"""The gen-2 fleet's SSH certificate authority contract, shared by every party that trusts or presents it.

Management SSH into a gen-2 box, slice VM, or agent container is
authenticated by short-lived OpenSSH user certificates signed by the tier's
CA in Vault (imbue-ai/mngr-internal#850), never by a long-lived key. Every
sshd we run trusts the tier CA through ``TrustedUserCAKeys`` and maps a
certificate's principals to local users through ``AuthorizedPrincipalsFile``.
The principals, the on-host paths, and the Vault mount / role names are
defined once here; the box prep, the VM cloud-init, the container setup, the
operator tooling, and the connector all render from it. Like the rest of this
subpackage it imports nothing beyond the stdlib, pydantic, and imbue_common:
it ships into the connector container.
"""

from collections.abc import Mapping
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import EmptySshCaPublicKeyError

# Certificate principals, one per management role. A user account on a host
# accepts exactly the principals its principals file lists, so a certificate's
# principal set is its reach: the connector never carries the operator
# principal, and analytics never carries the service (box) one.
SSH_CA_PRINCIPAL_OPERATOR: Final[str] = "mngr-operator"
SSH_CA_PRINCIPAL_SERVICE: Final[str] = "mngr-service"
SSH_CA_PRINCIPAL_VM: Final[str] = "mngr-vm"
SSH_CA_PRINCIPAL_CONTAINER: Final[str] = "mngr-container"

# What each Vault signing role may mint.
SSH_CA_OPERATOR_PRINCIPALS: Final[tuple[str, ...]] = (
    SSH_CA_PRINCIPAL_OPERATOR,
    SSH_CA_PRINCIPAL_SERVICE,
    SSH_CA_PRINCIPAL_VM,
    SSH_CA_PRINCIPAL_CONTAINER,
)
SSH_CA_CONNECTOR_PRINCIPALS: Final[tuple[str, ...]] = (
    SSH_CA_PRINCIPAL_SERVICE,
    SSH_CA_PRINCIPAL_VM,
    SSH_CA_PRINCIPAL_CONTAINER,
)
SSH_CA_ANALYTICS_PRINCIPALS: Final[tuple[str, ...]] = (SSH_CA_PRINCIPAL_VM, SSH_CA_PRINCIPAL_CONTAINER)

# The Vault SSH secrets engine roles (one mount per tier, ``minds-<tier>-ssh``;
# the ``minds-`` prefix leaves room for other SSH CAs in the same Vault).
SSH_CA_VAULT_ROLE_OPERATOR: Final[str] = "operator"
SSH_CA_VAULT_ROLE_CONNECTOR: Final[str] = "connector"
SSH_CA_VAULT_ROLE_ANALYTICS: Final[str] = "analytics"

# On-host trust material. The drop-in lands in the directory Debian's stock
# sshd_config already includes, so no sshd_config edit is needed anywhere.
SSH_CA_PUBLIC_KEY_PATH: Final[str] = "/etc/ssh/mngr_user_ca.pub"
SSH_CA_SSHD_DROP_IN_PATH: Final[str] = "/etc/ssh/sshd_config.d/61-mngr-user-ca.conf"
SSH_CA_PRINCIPALS_DIR: Final[str] = "/etc/ssh/principals"

# The management bootstrap user on a box (the OS image's sudo user) and the
# accounts a certificate reaches on the VM and in the container.
SSH_CA_BOX_BOOTSTRAP_USER: Final[str] = "debian"
SSH_CA_ROOT_USER: Final[str] = "root"


class SshTrustFile(FrozenModel):
    """One file an sshd host needs on disk to trust the tier CA (path, content, octal mode)."""

    path: str = Field(description="Absolute path on the host")
    content: str = Field(description="File content, newline-terminated")
    mode: str = Field(description="Octal permission bits, e.g. 0644")


@pure
def ssh_ca_vault_mount(tier: str) -> str:
    """The Vault SSH secrets engine mount holding ``tier``'s CA."""
    return f"minds-{tier}-ssh"


@pure
def ssh_ca_principals_file_path(user: str) -> str:
    return f"{SSH_CA_PRINCIPALS_DIR}/{user}"


@pure
def render_ssh_ca_sshd_drop_in() -> str:
    """The sshd drop-in: trust the CA file, resolve principals per user, and refuse password logins.

    ``%u`` is the user being authenticated; a user with no principals file
    accepts no certificate at all, so accounts we never list stay closed to
    every certificate. Password and keyboard-interactive authentication are
    switched off here too: every host that trusts the CA authenticates by key
    or certificate only, and Debian's stock sshd_config leaves both on.
    """
    return (
        f"TrustedUserCAKeys {SSH_CA_PUBLIC_KEY_PATH}\n"
        f"AuthorizedPrincipalsFile {SSH_CA_PRINCIPALS_DIR}/%u\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
    )


@pure
def ssh_ca_trust_files(ca_public_key: str, principals_by_user: Mapping[str, str]) -> tuple[SshTrustFile, ...]:
    """The files that make an sshd trust ``ca_public_key`` for the given user -> principal mapping."""
    if not ca_public_key.strip():
        raise EmptySshCaPublicKeyError("the SSH CA public key must not be empty")
    files = [
        SshTrustFile(path=SSH_CA_PUBLIC_KEY_PATH, content=ca_public_key.strip() + "\n", mode="0644"),
        SshTrustFile(path=SSH_CA_SSHD_DROP_IN_PATH, content=render_ssh_ca_sshd_drop_in(), mode="0644"),
    ]
    for user, principal in sorted(principals_by_user.items()):
        files.append(SshTrustFile(path=ssh_ca_principals_file_path(user), content=principal + "\n", mode="0644"))
    return tuple(files)


@pure
def render_ssh_ca_trust_shell_section(ca_public_key: str, principals_by_user: Mapping[str, str]) -> str:
    """Idempotent root bash that installs the CA trust files (heredocs; no sshd restart)."""
    lines = [f"mkdir -p {SSH_CA_PRINCIPALS_DIR} /etc/ssh/sshd_config.d"]
    for trust_file in ssh_ca_trust_files(ca_public_key, principals_by_user):
        lines.append(f"cat > {trust_file.path} <<'MNGR_SSH_CA_FILE'\n{trust_file.content}MNGR_SSH_CA_FILE")
        lines.append(f"chmod {trust_file.mode} {trust_file.path}")
    return "\n".join(lines) + "\n"


@pure
def is_same_ssh_public_key(left: str, right: str) -> bool:
    """Whether two OpenSSH public key lines carry the same key (type + base64; comments ignored)."""
    left_fields = left.split()
    right_fields = right.split()
    return len(left_fields) >= 2 and len(right_fields) >= 2 and left_fields[:2] == right_fields[:2]
