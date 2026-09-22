import pytest
from inline_snapshot import snapshot

from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr_latchkey.docker_bridge import BRIDGE_SERVICES_FIREWALL_UNIT_NAME
from imbue.mngr_latchkey.docker_bridge import BRIDGE_SERVICES_NFT_POLICY_PATH
from imbue.mngr_latchkey.docker_bridge import DockerBridgeAddressError
from imbue.mngr_latchkey.docker_bridge import DockerBridgeFirewallError
from imbue.mngr_latchkey.docker_bridge import build_bridge_services_nftables_policy
from imbue.mngr_latchkey.docker_bridge import ensure_bridge_services_firewalled
from imbue.mngr_latchkey.docker_bridge import resolve_docker_bridge_address
from imbue.mngr_latchkey.remote.mock_outer_host_test import as_stub
from imbue.mngr_latchkey.remote.mock_outer_host_test import stub_outer


def test_resolve_docker_bridge_address_returns_the_probed_address() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    as_stub(outer).docker_bridge_address = "172.18.0.1"
    assert resolve_docker_bridge_address(outer) == "172.18.0.1"
    command = as_stub(outer).recorded[-1].command
    assert "addr show docker0" in command


def test_resolve_docker_bridge_address_raises_when_the_probe_fails() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="ip: command not found", success=False))
    with pytest.raises(DockerBridgeAddressError, match="ip: command not found"):
        resolve_docker_bridge_address(outer)


@pytest.mark.parametrize("answer", ["", "0.0.0.0", "::", "[::]", "*"])
def test_resolve_docker_bridge_address_refuses_a_wildcard_answer(answer: str) -> None:
    # Failing closed: a wildcard is what a caller would end up binding if it
    # took the answer at face value, and on a VPS that is the public interface.
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    as_stub(outer).docker_bridge_address = answer
    with pytest.raises(DockerBridgeAddressError, match="Could not resolve a docker bridge address"):
        resolve_docker_bridge_address(outer)


def test_build_bridge_services_nftables_policy_confines_the_ports_to_the_bridge_and_the_bridge_to_the_ports() -> None:
    assert build_bridge_services_nftables_policy((1989, 8794)) == snapshot("""\
#!/usr/sbin/nft -f
# Managed by mngr (mngr_latchkey remote provisioning).
# The services the VPS binds on its docker bridge address (the latchkey gateway
# and the owner-exec daemon) answer only the agent's container and the VPS's
# own processes: a packet for their ports arriving on any other interface (the
# public one, a user-defined docker network) drops here, before the bound
# socket ever receives it. And the container reaches only those services: a new
# connection arriving from the bridge for any other port on this host drops too.
add table inet mngr_bridge_services
delete table inet mngr_bridge_services
add table inet mngr_bridge_services {
    chain input {
        type filter hook input priority filter; policy accept;
        iifname != "docker0" iifname != "lo" tcp dport { 1989, 8794 } counter drop
        iifname "docker0" tcp dport { 1989, 8794 } accept
        iifname "docker0" ct state new counter drop
    }
}
""")


def test_ensure_bridge_services_firewalled_installs_nftables_then_writes_and_applies_the_policy() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    ensure_bridge_services_firewalled(outer, (1989, 8794))
    commands = as_stub(outer).recorded_commands()
    assert "apt-get install -y nftables" in commands[0]
    assert "if [ ! -x /usr/sbin/nft ]" in commands[0]
    assert as_stub(outer).recorded[0].timeout_seconds == 300.0
    policy = next(w for w in as_stub(outer).written if w.path == str(BRIDGE_SERVICES_NFT_POLICY_PATH))
    assert "tcp dport { 1989, 8794 } counter drop" in policy.content.decode("utf-8")
    assert policy.mode == "0644"
    assert policy.is_atomic is True
    unit = next(w for w in as_stub(outer).written if w.path.endswith(f"{BRIDGE_SERVICES_FIREWALL_UNIT_NAME}.service"))
    unit_text = unit.content.decode("utf-8")
    assert f"ExecStart=/usr/sbin/nft -f {BRIDGE_SERVICES_NFT_POLICY_PATH}" in unit_text
    assert "Type=oneshot" in unit_text
    assert "RemainAfterExit=yes" in unit_text
    assert "After=nftables.service" in unit_text
    assert "Before=docker.service owner-exec-vm.service supervisor.service" in unit_text
    assert "WantedBy=multi-user.target" in unit_text
    assert commands[1] == (
        f"systemctl daemon-reload && systemctl enable --now {BRIDGE_SERVICES_FIREWALL_UNIT_NAME} "
        f"&& systemctl restart {BRIDGE_SERVICES_FIREWALL_UNIT_NAME}"
    )
    assert len(commands) == 2


def test_ensure_bridge_services_firewalled_raises_when_nftables_cannot_be_installed() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="E: Unable to locate package nftables", success=False))
    with pytest.raises(DockerBridgeFirewallError, match="install nftables.*Unable to locate package"):
        ensure_bridge_services_firewalled(outer, (1989,))
    assert as_stub(outer).written == []
