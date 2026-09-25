"""Provision the latchkey gateway on a remote VPS.

Agents on a remote host get the gateway managed here, while agents on a local
host reach the desktop gateway directly. The VPS gateway handles third-party
calls itself and forwards the desktop-owned endpoint families back to the desktop.

The VPS-resident gateway binds the VPS's docker bridge address, which the
agent's container reaches by the constant name ``host.docker.internal``: the
VPS provider creates every container with the matching ``--add-host`` mapping,
and the agent's ``LATCHKEY_GATEWAY`` names that host. Nothing bridges the two;
the docker bridge is the route, and the nftables policy the package loads
before either bridge-bound service starts keeps it the only route (see
:mod:`imbue.mngr_latchkey.docker_bridge`).

An agent whose ``LATCHKEY_GATEWAY`` names its own ``127.0.0.1`` instead still
reaches the gateway over a VPS->container reverse SSH tunnel the package runs
as a second ``supervisord`` program. That is every agent in a container
created without the mapping (``mngr create`` points such an agent at its
loopback whatever client runs it; see
:func:`imbue.mngr_latchkey.agent_setup.fall_back_to_reverse_tunneled_gateway_url`),
and every agent whose host was created by an older client while its container
already carried the mapping. Provisioning recognizes the first kind by the
missing ``--add-host`` mapping and the second by the tunnel keypair that client
had minted, so a tunnel once wired is never dropped (only re-pointed at the
address the gateway binds) and none is ever wired for a container that
resolves the outer host (see :func:`_does_container_need_reverse_tunnel`).

Everything the machine needs, other than its data, is one Debian package
(:mod:`imbue.mngr_latchkey.remote.package`), so the gateway's share of a
provisioning pass is five remote operations however much it has to set right
(the owner-exec daemon's own provisioning rides alongside): resolve the docker
bridge address (which that daemon binds too), upload the package, run the
bootstrap that installs it (its ``postinst`` installs the pinned latchkey CLI,
loads the policy that fences the bridge-bound ports off every other interface,
registers the gateway and the reverse tunnel as ``supervisord`` programs, and
scrubs what the ad-hoc provisioning left on a machine it set up), read what
the machine holds
(``mngr-latchkey read-state``, which also inspects how the agent's container
was created), and hand it what it should hold (``mngr-latchkey apply-state``):
the secrets its gateway runs under, the address it binds, its config, the
policy to seed a fresh machine with, and -- only for a container that needs it
-- the container to tunnel into. Which secrets those are is decided here, from
what the read found: the machine's own key and listen password are adopted
from a machine already running under them, while the pair its forwarding
extension presents to this computer is always this computer's. A machine
holding a store that neither it nor this computer knows the key to has the
desktop's key tried against that store before the store is given up on; the
read hands the store over whenever this computer has no key recorded for the
machine, and the key is tried here, so that costs no extra operation and the
desktop's key reaches the machine only once it is known to be the machine's
own.

Crash recovery deliberately stops short of surviving a full *reboot*: the
gateway's secrets are kept in a tmpfs directory under ``/run``, which is
RAM-backed and so survives process crashes -- letting ``supervisord`` restart
the gateway without a desktop round-trip -- but is wiped by a reboot. This is a
deliberate choice to never persist the encryption key on the VPS disk beside
the encrypted credential store (which would be equivalent to storing the
credentials in plaintext from a disk-snapshot threat model). The package's
scripts verify the directory really is RAM-backed and refuse to proceed
otherwise. After a reboot the gateway stays down until the next provisioning
pass re-writes the secrets.
"""

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OUTER_HOST_HOSTNAME_IN_CONTAINER
from imbue.mngr_latchkey.core import EncryptedCredentialStore
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import custom_service_registration_entries
from imbue.mngr_latchkey.core import merge_minds_latchkey_config
from imbue.mngr_latchkey.docker_bridge import DockerBridgeAddressError
from imbue.mngr_latchkey.docker_bridge import resolve_docker_bridge_address
from imbue.mngr_latchkey.encryption_key import LatchkeyEncryptionKeyPermissionError
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.owner_exec_vm import provision_owner_exec_vm

# Re-exported (the redundant alias marks it as such): the desktop app's host
# diagnostics name the machine's latchkey directory through it, to tail the
# gateway's logs there on a host not yet re-provisioned by a build that logs
# under :data:`REMOTE_LOG_DIR`.
from imbue.mngr_latchkey.remote._machine import REMOTE_LATCHKEY_DIR_NAME as REMOTE_LATCHKEY_DIR_NAME
from imbue.mngr_latchkey.remote._machine import RemoteMachineState
from imbue.mngr_latchkey.remote._machine import RemoteStateRequest
from imbue.mngr_latchkey.remote._machine import RemoteStateUpdate
from imbue.mngr_latchkey.remote._machine import RemoteTunnelTarget
from imbue.mngr_latchkey.remote._machine import apply_remote_state
from imbue.mngr_latchkey.remote._machine import read_remote_state
from imbue.mngr_latchkey.remote._mirror import generate_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import store_machine_gateway_password
from imbue.mngr_latchkey.remote._mirror import stored_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import stored_machine_gateway_password
from imbue.mngr_latchkey.remote._transfer import adopt_machine_permissions
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError

# Re-exported (the redundant alias marks them as such): the pins and locations
# consumers outside this plugin (the desktop app, its version-alignment tests, the
# bump-latchkey skill) refer to through this module.
from imbue.mngr_latchkey.remote.package import CONTAINER_TUNNEL_KEY_FILENAME as CONTAINER_TUNNEL_KEY_FILENAME
from imbue.mngr_latchkey.remote.package import CURL_SHIMS_SHA256_BY_TRIPLE as CURL_SHIMS_SHA256_BY_TRIPLE
from imbue.mngr_latchkey.remote.package import CURL_SHIMS_VERSION as CURL_SHIMS_VERSION
from imbue.mngr_latchkey.remote.package import DEFAULT_REMOTE_PACKAGE_LAYOUT as DEFAULT_REMOTE_PACKAGE_LAYOUT
from imbue.mngr_latchkey.remote.package import DESKTOP_GATEWAY_VPS_PORT as DESKTOP_GATEWAY_VPS_PORT
from imbue.mngr_latchkey.remote.package import GATEWAY_PROGRAM_NAME as GATEWAY_PROGRAM_NAME
from imbue.mngr_latchkey.remote.package import LATCHKEY_VERSION as LATCHKEY_VERSION
from imbue.mngr_latchkey.remote.package import OUTER_PORT as OUTER_PORT
from imbue.mngr_latchkey.remote.package import REMOTE_GATEWAY_LOG_FILENAME as REMOTE_GATEWAY_LOG_FILENAME
from imbue.mngr_latchkey.remote.package import REMOTE_TUNNEL_LOG_FILENAME as REMOTE_TUNNEL_LOG_FILENAME
from imbue.mngr_latchkey.remote.package import RemotePackageArtifact
from imbue.mngr_latchkey.remote.package import RemotePackageContext
from imbue.mngr_latchkey.remote.package import RemotePackageLayout
from imbue.mngr_latchkey.remote.package import TUNNEL_CONF_FILENAME as TUNNEL_CONF_FILENAME
from imbue.mngr_latchkey.remote.package import TUNNEL_PROGRAM_NAME as TUNNEL_PROGRAM_NAME
from imbue.mngr_latchkey.remote.package import build_remote_package
from imbue.mngr_latchkey.remote.package import remote_package_context
from imbue.mngr_latchkey.remote.package import render_bootstrap_script
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

# Where supervisord writes the gateway's and the tunnel's logs on the machine;
# consumers outside this plugin tail them there.
REMOTE_LOG_DIR: Final[Path] = DEFAULT_REMOTE_PACKAGE_LAYOUT.log_dir

# Generous wall-clock ceiling: ``apt-get update`` + a NodeSource install +
# ``npm install -g`` on a cold VPS routinely runs into the low minutes.
_INSTALL_TIMEOUT_SECONDS: Final[float] = 300.0

# If the install round-trip exceeds this, something is degrading (slow apt
# mirror, slow npm registry) even though it eventually succeeded; warn so we
# notice before it turns into an outright timeout.
_SLOW_INSTALL_WARNING_THRESHOLD_SECONDS: Final[float] = 90.0


class DesktopGatewaySecrets(FrozenModel):
    """What the machine's forwarding extension presents to the desktop gateway it proxies to.

    Both belong to the computer that is currently connected, not to the machine:
    the password is that computer's own gateway listen password, and the JWT is
    signed by its encryption key and names a path on its disk. So a provisioning
    pass overwrites both -- which is how an agent created from one of the
    user's computers keeps reaching the desktop-owned endpoint families after
    the user moves to another -- while the machine's own key and listen password
    are adopted rather than replaced.

    They are handed to the extension as files it reads per request, so
    overwriting them is enough: the gateway does not have to be restarted for
    the new computer's values to take effect.
    """

    gateway_password: str = Field(description="The desktop gateway's own listen password.")
    permissions_override: str = Field(
        description="A JWT targeting the host's permissions file on the desktop that minted it."
    )


class _MachineKeyDecision(FrozenModel):
    """The key a machine's gateway is to run under, and whether its store had to be given up for it."""

    key: SecretStr = Field(description="The key the machine's store is (or is about to be) written under.")
    is_store_abandoned: bool = Field(
        description="Whether the machine's store is to be removed: it was written under a key nobody present holds."
    )
    is_decided_here: bool = Field(
        description=(
            "Whether this pass decided the key, rather than adopting the one the machine runs under or handing "
            "back the one recorded here. A decided key is recorded only once the machine has taken it."
        )
    )


def provision_remote_gateway(
    host: OuterHostInterface,
    host_id: HostId,
    container_ssh_user: str,
    container_ssh_port: int,
    latchkey: Latchkey,
    desktop_secrets: DesktopGatewaySecrets,
    package_layout: RemotePackageLayout,
) -> None:
    """Stand up a VPS-resident latchkey gateway where the agent's container can reach it.

    Installs the package on the agent's outer host (the VPS) -- whose
    ``postinst`` loads the nftables policy that fences the bridge-bound ports
    off every interface but the docker bridge and loopback, before either
    service it protects is started -- then reads the machine and hands it what
    it should hold in one update: this machine's own
    encryption key (so it can decrypt the credentials it is given) and its own
    listen password (so it accepts the traffic of the agents on it) -- both
    adopted from a machine already running under them -- plus ``desktop_secrets``
    for the forwarding extension's hop to this computer, the VPS's docker
    bridge address for the gateway to bind (which the container reaches as
    ``host.docker.internal``, so nothing else is wired for it), this plugin's
    config, and the policy to seed a machine that has none. supervisord keeps
    the gateway running and restarts it on failure. A machine whose policy the
    read found is adopted here afterwards, since another of the user's
    computers may have granted something this one has never seen.

    An agent whose ``LATCHKEY_GATEWAY`` names its own loopback instead (see
    :func:`_does_container_need_reverse_tunnel` for how that is recognized)
    also gets the VPS->container reverse tunnel wired in the same update,
    pointed at the address the gateway binds: its container is located on the
    VPS by its host-id label and reached with the inner host's ssh user and the
    port its sshd is published on from the VPS's own loopback.

    A gateway with no permissions file at all permits everything, which is why
    the seed happens here rather than waiting for someone to open the
    host's Permissions tab.

    Only genuinely-remote outer hosts are provisioned: when ``host`` is the
    local machine (e.g. the outer of a local docker daemon) this is a no-op, so
    we never apt/npm-install latchkey or run a gateway on the user's own
    computer. ``latchkey`` is this computer's, whose directory holds what is
    recorded about the machine and whose CLI tries a key against the machine's
    store. ``package_layout`` is where the package's files live on the machine
    (:data:`~imbue.mngr_latchkey.remote.package.DEFAULT_REMOTE_PACKAGE_LAYOUT`
    on a real one). Raises :class:`RemoteGatewayError` if any step fails.
    """
    if host.is_local:
        logger.debug(
            "Skipping remote latchkey gateway provisioning: outer host {} is local, not a remote VPS",
            host.get_name(),
        )
        return
    # The VM-resident owner-exec daemon is stood up on this pass too: it is
    # independent of the latchkey gateway (the web client configures the VM
    # through it, including to provision latchkey), and piggybacking on this
    # pass is how every remote provider converges on the one exec channel. A
    # failure of either fails the whole pass, which is retried on the next
    # discovery cycle.
    listen_host = _resolve_bridge_listen_host(host)
    # The package first: its postinst loads the policy that keeps both
    # bridge-bound services on the docker bridge, which has to be in place
    # before the daemon below (or the gateway) listens.
    context = remote_package_context(package_layout)
    _install_remote_package(host, context, build_remote_package(context))
    provision_owner_exec_vm(host, host_id, listen_host)
    latchkey_directory = latchkey.latchkey_directory
    recorded_key = _recorded_machine_encryption_key(latchkey_directory, host_id)
    # Without a recorded key, deciding one may mean trying the desktop's key
    # against the machine's store, so the store comes back with this read.
    state = read_remote_state(
        host,
        RemoteStateRequest(container_host_id=host_id, is_credential_store_included=recorded_key is None),
        failure_description=f"read the latchkey state of host {host_id}",
    )
    key_decision = _resolve_machine_encryption_key(latchkey, host_id, state, recorded_key)
    listen_password = _resolve_machine_gateway_password(
        latchkey_directory, host_id, state, desktop_secrets.gateway_password
    )
    # CLEANUP: drop the tunnel target together with the reverse tunnel; see the
    # comment above ``_does_container_need_reverse_tunnel``.
    tunnel = (
        RemoteTunnelTarget(host_id=host_id, ssh_user=container_ssh_user, ssh_port=container_ssh_port)
        if _does_container_need_reverse_tunnel(host, host_id, state)
        else None
    )
    update = RemoteStateUpdate(
        is_credential_store_abandoned=key_decision.is_store_abandoned,
        encryption_key=key_decision.key,
        listen_password=listen_password,
        desktop_gateway_password=desktop_secrets.gateway_password,
        desktop_permissions_override=desktop_secrets.permissions_override,
        config_json=_merged_remote_config(host, latchkey_directory, state),
        permissions_json=_permissions_to_seed(latchkey_directory, host_id, state),
        gateway_listen_host=listen_host,
        tunnel=tunnel,
        is_gateway_restarted=True,
    )
    with log_span("Applying the latchkey gateway state of host {} to VPS {}", host_id, host.get_name()):
        apply_remote_state(host, update, failure_description=f"provision the latchkey gateway of host {host_id}")
    if key_decision.is_decided_here:
        _record_machine_encryption_key(latchkey_directory, host_id, key_decision.key)
    if state.permissions_json is not None:
        adopt_machine_permissions(latchkey_directory, host_id, state.permissions_json)


def _install_remote_package(
    host: OuterHostInterface, context: RemotePackageContext, artifact: RemotePackageArtifact
) -> None:
    """Upload the package and run the bootstrap that installs it (and what it depends on).

    Idempotent: the bootstrap installs the Debian dependencies and Node.js only
    when missing (or, for Node.js, too old), and reinstalling the package
    re-runs its ``postinst``, which repairs whatever a previous pass left
    unfinished and re-applies the bridge-services firewall (a machine the
    policy cannot be loaded on fails the install, so nothing is started
    unfenced). The read that follows is what makes the install verifiable: it
    runs the package's own command. Raises :class:`RemoteGatewayError` if the
    install fails.
    """
    host_name = host.get_name()
    artifact_path = context.layout.artifact_dir / artifact.filename
    with log_span("Installing latchkey gateway package {} on VPS {}", artifact.version, host_name):
        # Atomic, so the rename replaces whatever another local user may have
        # planted at this predictable path in the world-writable /tmp with a
        # root-owned file dpkg then reads as uploaded.
        host.write_file(artifact_path, artifact.content, is_atomic=True)
        started_at = time.monotonic()
        result = host.execute_idempotent_command(
            render_bootstrap_script(context, artifact), timeout_seconds=_INSTALL_TIMEOUT_SECONDS
        )
        elapsed_seconds = time.monotonic() - started_at
    if not result.success:
        raise RemoteGatewayError(
            "Failed to install latchkey gateway package {} on VPS {}: {}".format(
                artifact.version, host_name, result.stderr.strip() or result.stdout.strip()
            )
        )
    if elapsed_seconds > _SLOW_INSTALL_WARNING_THRESHOLD_SECONDS:
        logger.warning(
            "Installing the latchkey gateway package on VPS {} took {:.0f}s",
            host_name,
            elapsed_seconds,
        )


def _resolve_bridge_listen_host(host: OuterHostInterface) -> str:
    """The VPS's docker bridge address, which the gateway and the owner-exec daemon bind.

    Resolved once per provisioning pass for both. Raises
    :class:`RemoteGatewayError` rather than falling back to a wildcard bind,
    which on a VPS would put either service on the public interface.
    """
    try:
        return resolve_docker_bridge_address(host)
    except DockerBridgeAddressError as e:
        raise RemoteGatewayError(
            "Refusing to bind the latchkey gateway and the owner-exec daemon on VPS {} to a public/wildcard "
            "interface: {}".format(host.get_name(), e)
        ) from e


@pure
def _do_extra_hosts_resolve_outer_host(extra_hosts: Sequence[str]) -> bool:
    """Whether a container's creation-time ``--add-host`` mappings (its inspect's ``HostConfig.ExtraHosts``) name the outer host."""
    return any(entry.split(":", 1)[0] == OUTER_HOST_HOSTNAME_IN_CONTAINER for entry in extra_hosts)


# CLEANUP: drop this decision together with the reverse tunnel; see the comment
# above ``CONTAINER_TUNNEL_KEY_FILENAME`` in ``remote/package.py``.
def _does_container_need_reverse_tunnel(host: OuterHostInterface, host_id: HostId, state: RemoteMachineState) -> bool:
    """Whether the agent in the host's container reaches the gateway only over the reverse tunnel.

    The agent's ``LATCHKEY_GATEWAY`` was decided by the client that created its
    host and cannot change for the life of the container, while the container's
    ``host.docker.internal`` mapping is decided by the outer host it was created
    on. A current client makes the two agree at create time (an agent in a
    container without the mapping names its own loopback; see
    :func:`imbue.mngr_latchkey.agent_setup.fall_back_to_reverse_tunneled_gateway_url`),
    leaving one exception: a host an older client created in a container that
    already carried the mapping. Its agent names its own loopback, and that
    client wired the tunnel for it, minting the keypair the tunnel presents.
    So the tunnel is needed when the container cannot resolve the outer host at
    all (the read found no such mapping among the ones it was created with),
    or when the machine holds a tunnel keypair; a tunnel once wired is never
    dropped (a container recreated on the same VPS keeps a redundant one, which
    is harmless), and none is wired for a container that resolves the outer
    host. Raises :class:`RemoteGatewayError` if the read did not look at the
    container.
    """
    if state.container_extra_hosts is None:
        raise RemoteGatewayError(
            f"Cannot decide the gateway route of host {host_id} on VPS {host.get_name()}: the machine was not asked "
            "how its container was created"
        )
    if not _do_extra_hosts_resolve_outer_host(state.container_extra_hosts):
        logger.info(
            "Container of host {} on VPS {} predates the {} mapping; keeping its latchkey reverse tunnel",
            host_id,
            host.get_name(),
            OUTER_HOST_HOSTNAME_IN_CONTAINER,
        )
        return True
    if state.has_container_tunnel_key:
        logger.info(
            "Container of host {} on VPS {} resolves {} but an earlier provisioning wired a latchkey reverse "
            "tunnel into it (its agent may name its own loopback); keeping the tunnel",
            host_id,
            host.get_name(),
            OUTER_HOST_HOSTNAME_IN_CONTAINER,
        )
        return True
    return False


def _merged_remote_config(host: OuterHostInterface, latchkey_directory: Path, state: RemoteMachineState) -> str:
    """This plugin's hidden services and service registrations, merged into the machine's config.

    The same merge the desktop applies to its own config
    (:func:`~imbue.mngr_latchkey.core.merge_minds_latchkey_config`), so an agent
    talking to the VPS-resident gateway sees the same hidden built-in services
    as one talking to the desktop gateway, and the VPS gateway knows both the
    bundled additional services and the user-created custom ones this
    computer's config registers (the machine's holds none of its own). The
    registration is what makes a custom service's credentials usable: a
    gateway with no matching registration cannot resolve a request to that
    service at all, so it would never inject them. Services created later
    reach the machine as the config snapshot every connect carries (see
    :meth:`~imbue.mngr_latchkey.remote.credentials.MachineCredentials._config_for`).
    Any other config latchkey wrote on the machine is preserved. Raises
    :class:`RemoteGatewayError` if the existing remote config is not a valid
    JSON object.
    """
    try:
        return merge_minds_latchkey_config(state.config_json, custom_service_registration_entries(latchkey_directory))
    except LatchkeyError as e:
        raise RemoteGatewayError(f"Failed to update the latchkey config on VPS {host.get_name()}: {e}") from e


def _permissions_to_seed(latchkey_directory: Path, host_id: HostId, state: RemoteMachineState) -> str | None:
    """The policy to hand a machine that has none yet, or ``None`` for a machine that has one.

    A machine's policy lives on the machine (``~/.latchkey/permissions.json``),
    which is what lets a second computer see what the first one granted -- it
    reads the machine rather than a copy it never had. This computer keeps the
    canonical file at
    ``<latchkey_directory>/mngr_latchkey/hosts/<host_id>/latchkey_permissions.json``,
    which is the one the permission UI edits and the gateway extension writes.

    Edits made here are pushed to the machine when they are made
    (:meth:`~imbue.mngr_latchkey.remote.credentials.MachineCredentials.set_permissions`),
    so this never has to guess which side is newer: a machine that has a policy
    keeps it (and the caller adopts it, exactly as an ordinary refresh does).
    The one write toward the machine is the seed: a machine with no policy yet
    gets this computer's copy -- or the restrictive deny-all default, so a host
    with no explicit grants still gets a locked-down gateway.

    Raises :class:`RemoteGatewayError` if this computer's copy cannot be read.
    """
    if state.permissions_json is not None:
        return None
    local_path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    if local_path.is_file():
        try:
            return local_path.read_text()
        except OSError as e:
            raise RemoteGatewayError(f"Failed to read host permissions file {local_path}: {e}") from e
    logger.debug("No local permissions file for host {} at {}; using the restrictive default", host_id, local_path)
    return _default_permissions_json()


def _default_permissions_json() -> str:
    """Serialize the deny-all default permissions config (matches ``save_permissions`` output)."""
    config = LatchkeyPermissionsConfig()
    # ``save_permissions`` omits an empty ``schemas`` block; mirror it so the
    # remote file is byte-for-byte the same shape the plugin writes locally.
    exclude: set[str] = set()
    if not config.schemas:
        exclude.add("schemas")
    return config.model_dump_json(indent=2, exclude=exclude)


def _recorded_machine_encryption_key(latchkey_directory: Path, host_id: HostId) -> SecretStr | None:
    """The key this computer recorded for the machine, or ``None`` when it has none.

    Raises:
        RemoteGatewayError: when the record exists but cannot be read.
    """
    try:
        return stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id)
    except LatchkeyStoreError as e:
        raise RemoteGatewayError(f"Failed to read the recorded encryption key of host {host_id}: {e}") from e


def _resolve_machine_encryption_key(
    latchkey: Latchkey, host_id: HostId, state: RemoteMachineState, recorded_key: SecretStr | None
) -> _MachineKeyDecision:
    """Return the encryption key this machine keeps its own credential store under.

    The machine's own copy -- the tmpfs file its gateway reads, which the read
    found -- is the source of truth while it exists: any of the user's
    computers may have provisioned the machine, so what this computer recorded
    is only a durable *mirror* of the machine's key, kept so a rebooted machine
    (whose tmpfs is wiped) can be handed its key back. Adopting the running
    key, rather than deciding one, is what lets two desktops manage one
    machine without re-keying it out from under each other.

    Only when neither the machine nor this computer knows a key is one decided
    (see :func:`_decide_key_for_machine_with_no_known_key`). A decided key is
    not recorded here: the caller records it once the machine has taken it
    (:func:`_record_machine_encryption_key`), since the apply that hands it
    over is also what removes a store nobody can read, and a record written
    ahead of a failed apply would have the next pass hand the machine a key its
    store was never written under.

    Raises:
        RemoteGatewayError: when the key cannot be read, decided, or recorded.
    """
    data_dir = plugin_data_dir(latchkey.latchkey_directory)
    try:
        if state.encryption_key is not None:
            if recorded_key is None or recorded_key.get_secret_value() != state.encryption_key.get_secret_value():
                store_machine_encryption_key(data_dir, host_id, state.encryption_key)
                logger.info("Adopted the encryption key the machine of host {} is already running under", host_id)
            return _MachineKeyDecision(key=state.encryption_key, is_store_abandoned=False, is_decided_here=False)
        if recorded_key is not None:
            # The machine rebooted (or has never run a gateway); hand it back
            # the key its store is already written under.
            return _MachineKeyDecision(key=recorded_key, is_store_abandoned=False, is_decided_here=False)
        decision = _decide_key_for_machine_with_no_known_key(latchkey, host_id, state)
    except (LatchkeyStoreError, LatchkeyEncryptionKeyPermissionError) as e:
        raise RemoteGatewayError(f"Failed to resolve the encryption key for host {host_id}: {e}") from e
    return decision


def _record_machine_encryption_key(latchkey_directory: Path, host_id: HostId, key: SecretStr) -> None:
    """Record the key the machine has just taken, so a rebooted machine can be handed it back.

    Raises:
        RemoteGatewayError: when the record cannot be written.
    """
    try:
        store_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id, key)
    except LatchkeyStoreError as e:
        raise RemoteGatewayError(f"Failed to record the encryption key of host {host_id}: {e}") from e


def _resolve_machine_gateway_password(
    latchkey_directory: Path,
    host_id: HostId,
    state: RemoteMachineState,
    desktop_gateway_password: str,
) -> str:
    """Return the listen password this machine's gateway holds its callers to.

    Adopted, never decided, for a blunter reason than the encryption key is
    (:func:`_resolve_machine_encryption_key`): the agents on this machine
    present the password from a host env file written once, at ``mngr create``,
    and nothing rewrites that file for the life of the host. Whichever of
    the user's computers created them therefore fixed the value, and a second
    computer that wrote its own here would answer every one of their requests
    with a 401.

    So the machine's running copy wins, this computer's record of it is the
    fallback for a machine whose tmpfs a reboot wiped, and only a machine that
    neither is running one nor has one recorded gets ``desktop_gateway_password``
    -- the value this computer bakes into the hosts it creates, and hence
    the right one for a machine it is about to provision for the first time.

    One combination cannot be served, and is why the record is worth writing on
    every pass: a machine whose password no computer has recorded yet, which
    rebooted (wiping its own copy) before this computer -- not the one that
    created its agents -- provisioned it. It is seeded with a password those
    agents do not hold, and every later pass then adopts that, since nothing
    anywhere still knows the value they were given. Their latchkey calls stay
    unauthorized; a build that records the password while the machine is still
    running closes the window for good.

    Raises:
        RemoteGatewayError: when the password cannot be read or recorded.
    """
    data_dir = plugin_data_dir(latchkey_directory)
    try:
        recorded_password = stored_machine_gateway_password(data_dir, host_id)
        if state.listen_password is not None:
            if state.listen_password != recorded_password:
                store_machine_gateway_password(data_dir, host_id, state.listen_password)
                logger.info(
                    "Adopted the gateway listen password the machine of host {} is already running under", host_id
                )
            return state.listen_password
        if recorded_password is not None:
            # The machine rebooted (or has never run a gateway); hand it back
            # the password its agents were created with.
            return recorded_password
        store_machine_gateway_password(data_dir, host_id, desktop_gateway_password)
    except LatchkeyStoreError as e:
        raise RemoteGatewayError(f"Failed to resolve the gateway listen password for host {host_id}: {e}") from e
    logger.info("Host {} will hold its callers to this computer's gateway listen password", host_id)
    return desktop_gateway_password


def _decide_key_for_machine_with_no_known_key(
    latchkey: Latchkey, host_id: HostId, state: RemoteMachineState
) -> _MachineKeyDecision:
    """Decide the key for a machine that is not running one, with none recorded here.

    A machine holding no credential store gets a fresh random key: there is
    nothing a key choice could make unreadable, and a key of its own means what
    the machine will hold is readable by that machine and by the desktops the
    user manages it from, and by nothing else.

    A machine that *does* hold a store can only be served by the key the store
    was written under, so the one candidate anyone here holds -- this desktop's
    key, which is what a build predating per-machine keys provisioned
    machines with -- is tried against the copy of the store the read brought
    back. When it opens the store, this is our own legacy machine, and the
    desktop key is kept so that what the machine holds stays readable. The
    desktop's key goes to the machine only then, once it is known to be the
    machine's own.

    When it does not, the store was written under a key nobody present holds:
    another computer provisioned this machine and the machine rebooted since.
    The store is abandoned -- removed, and a fresh key minted -- because signing
    the machine's services in again is always possible, while waiting for a
    computer that may never return is not.

    CLEANUP: drop the desktop-key check (abandoning every store no recorded key
    opens) once no machine provisioned before per-machine keys remains.
    """
    if not state.has_credential_store:
        logger.info("Host {} will keep its credentials under its own encryption key", host_id)
        return _MachineKeyDecision(
            key=generate_machine_encryption_key(), is_store_abandoned=False, is_decided_here=True
        )
    if state.credential_store is None:
        raise RemoteGatewayError(
            f"Cannot decide the encryption key of host {host_id}: the machine holds a credential store but the read "
            "did not bring it back"
        )
    desktop_key = load_or_create_encryption_key(latchkey.latchkey_directory)
    if _does_key_open_the_machine_store(latchkey, host_id, state.credential_store, desktop_key):
        logger.info(
            "Host {} keeps its credentials under the desktop's key, which opens its store "
            "(provisioned before per-machine keys)",
            host_id,
        )
        return _MachineKeyDecision(key=desktop_key, is_store_abandoned=False, is_decided_here=True)
    logger.warning(
        "Abandoning the credential store of host {}: it was written under a key this computer does not hold "
        "(provisioned by another computer, and the machine rebooted since); its services need signing in again",
        host_id,
    )
    return _MachineKeyDecision(key=generate_machine_encryption_key(), is_store_abandoned=True, is_decided_here=True)


def _does_key_open_the_machine_store(
    latchkey: Latchkey, host_id: HostId, machine_store: EncryptedCredentialStore, candidate_key: SecretStr
) -> bool:
    """Whether the machine's own credential store decrypts under ``candidate_key``.

    Tried here, against the copy of the store the read brought back, so the
    candidate never reaches the machine unless it turns out to be the key the
    store was written under.

    Raises:
        RemoteGatewayError: when the store cannot be tried, or fails for any
            reason other than the key (a caller abandons a store no candidate
            opens, so nothing else may read as a key that does not).
    """
    with log_span("Trying the desktop's key against the credential store of host {}", host_id):
        try:
            is_opened = latchkey.does_key_open_foreign_store(machine_store, candidate_key)
        except LatchkeyError as e:
            raise RemoteGatewayError(
                f"Failed to try the desktop's key against the credential store of host {host_id}: {e}"
            ) from e
    if not is_opened:
        logger.debug("Ruled out the desktop's key for the credential store of host {}", host_id)
    return is_opened
