import base64
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.providers.ssh_host_setup import JOURNALD_DROP_IN_PATH
from imbue.mngr_vps.container_setup import LABEL_HOST_ID
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.host_setup import CONTAINER_MEMORY_RESERVE_MIB
from imbue.mngr_vps.host_setup import CONTAINER_MEMORY_UNIT_NAME
from imbue.mngr_vps.host_setup import ContainerMemoryCapProbe
from imbue.mngr_vps.host_setup import MNGR_SSHD_DROP_IN_PATH
from imbue.mngr_vps.host_setup import PINNED_DOCKER_VERSION
from imbue.mngr_vps.host_setup import PINNED_GVISOR_BINARY_INSTALL_SCRIPT
from imbue.mngr_vps.host_setup import PINNED_GVISOR_RELEASE
from imbue.mngr_vps.host_setup import apply_host_setup_on_outer
from imbue.mngr_vps.host_setup import build_apply_container_memory_cap_command
from imbue.mngr_vps.host_setup import build_host_setup_steps
from imbue.mngr_vps.host_setup import build_remote_script_command
from imbue.mngr_vps.host_setup import expected_container_memory_cap_bytes
from imbue.mngr_vps.host_setup import parse_container_memory_cap_probe
from imbue.mngr_vps.host_setup import parse_mem_total_kib
from imbue.mngr_vps.host_setup import render_container_memory_reconcile_script
from imbue.mngr_vps.host_setup import render_gvisor_binary_install_script
from imbue.mngr_vps.host_setup import render_sshd_drop_in_stage_script
from imbue.mngr_vps.host_setup import render_vm_tmp_mount_unit


class _StubOuter(MutableModel):
    """Records each idempotent command and returns canned results from a FIFO."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    responses: list[CommandResult] = Field(default_factory=list, description="FIFO of responses; default-success")
    recorded_commands: list[str] = Field(default_factory=list, description="Each command recorded in order")

    def get_name(self) -> str:
        return "stub-outer"

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded_commands.append(command)
        if self.responses:
            return self.responses.pop(0)
        return CommandResult(stdout="", stderr="", success=True)


def _outer(*responses: CommandResult) -> OuterHostInterface:
    return cast(OuterHostInterface, _StubOuter(responses=list(responses)))


def _stub(outer: OuterHostInterface) -> _StubOuter:
    return cast(_StubOuter, outer)


def _decode_remote_command(command: str) -> str:
    """Recover the original script from an ``echo <b64> | base64 -d | sh`` command."""
    encoded = command.split(" | ", 1)[0].removeprefix("echo ")
    return base64.b64decode(encoded).decode("utf-8")


def test_build_host_setup_steps_minimal_order() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    descriptions = [step.description for step in steps]
    # The bounds step needs jq from the base packages and must precede the Docker
    # install so the daemon starts with the bounds.
    assert descriptions[0].startswith("Keep the VM's /tmp on its disk")
    assert descriptions[1].startswith("Install base packages")
    assert descriptions[2].startswith("Bound container logs")
    assert "Docker" in descriptions[3]
    assert descriptions[4].startswith("Install the sshd drop-in")
    assert descriptions[5].startswith("Install the every-boot container memory cap")
    assert len(descriptions) == 6
    assert not any("gVisor" in d or "runsc" in d for d in descriptions)
    assert not any("qemu" in d for d in descriptions)


def test_vm_tmp_mount_step_overrides_the_distro_tmpfs_and_restarts_only_when_tmp_is_not_the_bind() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    tmp_step = next(step for step in steps if step.description.startswith("Keep the VM's /tmp"))
    unit = render_vm_tmp_mount_unit()
    assert unit in tmp_step.script
    assert "What=/var/lib/mngr-tmp\nWhere=/tmp\nType=none\nOptions=bind,nosuid,nodev" in unit
    assert "WantedBy=local-fs.target" in unit
    assert "install -d -m 1777 /var/lib/mngr-tmp" in tmp_step.script
    # The unit is content-converged, and the bind is only (re)started when /tmp is
    # currently something else: the distro tmpfs on a first boot, nothing on a
    # Debian 12 image, and never again once the bind is in place.
    assert "cmp -s" in tmp_step.script
    assert tmp_step.script.index("cmp -s") < tmp_step.script.index("systemctl enable tmp.mount")
    assert "if ! findmnt -no SOURCE /tmp 2>/dev/null | grep -q '/var/lib/mngr-tmp\\]$'; then" in tmp_step.script
    assert "systemctl restart tmp.mount" in tmp_step.script
    assert tmp_step.script.rstrip().endswith("findmnt -no SOURCE /tmp | grep -q '/var/lib/mngr-tmp\\]$'")


def test_build_host_setup_steps_pins_docker_version() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    docker_step = next(step for step in steps if "Docker" in step.description)
    # The apt version is derived per-distro from /etc/os-release at run time (so
    # the same step works on Debian-family Vultr/OVH/AWS outers and on GCP's
    # Ubuntu), so assert the pinned core + the derivation rather than a literal.
    assert PINNED_DOCKER_VERSION in docker_step.script
    assert 'DOCKER_APT_VERSION="' in docker_step.script
    assert "~${ID}.${VERSION_ID}~${VERSION_CODENAME}" in docker_step.script
    assert 'docker-ce="${DOCKER_APT_VERSION}"' in docker_step.script
    assert "--allow-downgrades" in docker_step.script
    assert "get.docker.com" not in docker_step.script


def test_sshd_drop_in_step_installs_the_full_hardening_set_and_restarts_only_on_change() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    sshd_step = next(step for step in steps if step.description.startswith("Install the sshd drop-in"))
    assert MNGR_SSHD_DROP_IN_PATH == "/etc/ssh/sshd_config.d/60-mngr.conf"
    assert ">> /etc/ssh/sshd_config" not in sshd_step.script
    for line in (
        "MaxSessions 100",
        "MaxStartups 100:30:200",
        "PermitRootLogin prohibit-password",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
    ):
        assert line in sshd_step.script
    # OpenSSH >= 9.8 (Debian 13) penalizes unauthenticated connections per source
    # address, which locks out mngr's own probes; the keyword aborts older sshds,
    # so it is written only when the running sshd advertises it.
    assert "sshd -T 2>/dev/null | grep -qi '^persourcepenalties'" in sshd_step.script
    assert "printf 'PerSourcePenalties no\\n' >>" in sshd_step.script
    # The staged file is compared against the installed one, so a re-run (every GCE
    # boot, an OVH re-provision) restarts sshd only when the content changed.
    assert f'cmp -s "$MNGR_SSHD_STAGE/60-mngr.conf" {MNGR_SSHD_DROP_IN_PATH}' in sshd_step.script
    assert sshd_step.script.count("systemctl restart ssh 2>/dev/null") == 1
    assert sshd_step.script.index("cmp -s") < sshd_step.script.index("systemctl restart ssh")


def test_sshd_drop_in_stage_script_writes_to_the_given_path() -> None:
    # The GCE startup script stages the same content into its own temp dir, so
    # the renderer takes the destination and the two writers cannot drift.
    staged = render_sshd_drop_in_stage_script("$STAGE/60-mngr.conf")
    assert staged.count('"$STAGE/60-mngr.conf"') == 2
    assert staged.startswith("printf 'MaxSessions 100\\nMaxStartups 100:30:200\\n")


def test_docker_daemon_bounds_step_merges_daemon_json_and_restarts_only_on_change() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    bounds_step = next(step for step in steps if step.description.startswith("Bound container logs"))
    # Merged with jq (``runsc install`` also writes daemon.json), both sides
    # canonicalized so formatting differences never trigger a docker restart.
    assert "jq -S . /etc/docker/daemon.json" in bounds_step.script
    assert "'. * $bounds'" in bounds_step.script
    assert '"log-driver": "json-file"' in bounds_step.script
    assert '"max-size": "50m"' in bounds_step.script
    assert '"max-file": "3"' in bounds_step.script
    assert '"defaultKeepStorage": "1GB"' in bounds_step.script
    # Docker is restarted only when the merged file differs AND the daemon is
    # already running (on first boot it is installed afterwards).
    assert "if systemctl is-active --quiet docker; then systemctl restart docker; fi" in bounds_step.script
    assert bounds_step.script.index("cmp -s") < bounds_step.script.index("systemctl restart docker")
    assert JOURNALD_DROP_IN_PATH in bounds_step.script
    assert "SystemMaxUse=512M" in bounds_step.script
    assert bounds_step.script.count("systemctl restart systemd-journald") == 1


def test_container_memory_reconciler_step_installs_an_enabled_unit_guarded_on_change() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    memory_step = next(step for step in steps if "container memory cap" in step.description)
    assert render_container_memory_reconcile_script() in memory_step.script
    assert "ExecStart=/usr/local/sbin/mngr-vps-container-memory.sh" in memory_step.script
    assert "After=docker.service" in memory_step.script
    assert f"systemctl enable {CONTAINER_MEMORY_UNIT_NAME}" in memory_step.script
    assert memory_step.script.index("cmp -s") < memory_step.script.index("systemctl daemon-reload")


def test_container_memory_reconcile_script_caps_labelled_containers_at_mem_total_minus_the_reserve() -> None:
    script = render_container_memory_reconcile_script()
    assert script.startswith("#!/bin/sh\n")
    assert "awk '/^MemTotal:/{print $2}' /proc/meminfo" in script
    assert f"cap_mib=$(( total_kib / 1024 - {CONTAINER_MEMORY_RESERVE_MIB} ))" in script
    # Only VM-sized mngr agent containers are touched (an explicit --memory keeps
    # its cap), and swap is pinned to the cap so the container is shed under
    # pressure instead of thrashing.
    assert f'docker ps -aq --filter "label={LABEL_HOST_ID}" --filter "label=com.imbue.mngr.memory-cap=vm"' in script
    assert 'docker update --memory "${cap_mib}m" --memory-swap "${cap_mib}m"' in script
    assert 'if [ "$cap_mib" -le 0 ]; then exit 0; fi' in script


def test_apply_container_memory_cap_command_starts_the_unit_only_when_installed() -> None:
    command = build_apply_container_memory_cap_command("mngr-my-host")
    assert f"if [ -f /etc/systemd/system/{CONTAINER_MEMORY_UNIT_NAME} ]; then systemctl start" in command
    assert "echo mngr-memory-unit=absent" in command
    assert command.endswith("docker inspect --format '{{.HostConfig.Memory}}' mngr-my-host")


def test_parse_mem_total_kib_reads_one_integer_line() -> None:
    assert parse_mem_total_kib("8136000\n") == 8136000
    for bad_stdout in ("", "8136000\n1\n", "MemTotal: 8136000 kB\n"):
        with pytest.raises(VpsProvisioningError, match="MemTotal probe output"):
            parse_mem_total_kib(bad_stdout)


def test_parse_container_memory_cap_probe_returns_the_probe_when_the_unit_is_present() -> None:
    assert parse_container_memory_cap_probe("mngr-memory-unit=present\n8136000\n7516192768\n") == (
        ContainerMemoryCapProbe(mem_total_kib=8136000, container_memory_bytes=7516192768)
    )


def test_parse_container_memory_cap_probe_returns_none_when_the_unit_is_absent() -> None:
    assert parse_container_memory_cap_probe("mngr-memory-unit=absent\n8136000\n0\n") is None


@pytest.mark.parametrize("stdout", ["", "garbage\n1\n2\n", "mngr-memory-unit=present\nnot-a-number\n0\n"])
def test_parse_container_memory_cap_probe_rejects_unexpected_output(stdout: str) -> None:
    with pytest.raises(VpsProvisioningError, match="probe output"):
        parse_container_memory_cap_probe(stdout)


def test_expected_container_memory_cap_bytes_matches_the_reconcile_script_arithmetic() -> None:
    # An 8 GB GCE / Azure VM (MemTotal 8136000 KiB) is capped at 7945 - 1024 = 6921 MiB,
    # exactly what the shell's integer arithmetic produces.
    assert expected_container_memory_cap_bytes(8136000) == 6921 * 1024 * 1024
    assert expected_container_memory_cap_bytes(CONTAINER_MEMORY_RESERVE_MIB * 1024) is None
    assert expected_container_memory_cap_bytes(512 * 1024) is None


def test_build_host_setup_steps_includes_gvisor_when_requested() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=True, is_qemu_purge_enabled=False)
    gvisor_step = next(step for step in steps if "gVisor" in step.description)
    assert f"gvisor/releases/release/{PINNED_GVISOR_RELEASE}" in gvisor_step.script
    # runsc is registered with --overlay2=none so the container's writable layer
    # persists across a docker restart (the default per-sandbox overlay loses it).
    assert "runsc install -- --overlay2=none" in gvisor_step.script
    # The binary download is skipped when runsc is already present, and the
    # daemon (re)registration is skipped when the flag is already configured.
    assert "command -v runsc" in gvisor_step.script
    assert "--overlay2=none' /etc/docker/daemon.json" in gvisor_step.script


def test_build_host_setup_steps_includes_qemu_purge_when_requested() -> None:
    steps = build_host_setup_steps(install_gvisor_runtime=False, is_qemu_purge_enabled=True)
    qemu_step = next(step for step in steps if "qemu" in step.description)
    assert "apt-get purge --auto-remove -y 'qemu*'" in qemu_step.script
    assert "dpkg -l | grep -q qemu" in qemu_step.script


def test_build_host_setup_steps_excludes_ssh_host_key_injection() -> None:
    # SSH host-key injection is first-boot-only and must NOT be re-runnable, or a
    # re-provision would reset the VPS root host key and break known_hosts.
    steps = build_host_setup_steps(install_gvisor_runtime=True, is_qemu_purge_enabled=True)
    for step in steps:
        assert "ssh_deletekeys" not in step.script
        assert "ed25519_private" not in step.script
        assert "ssh_keys" not in step.script


def test_remote_script_command_round_trips() -> None:
    script = "set -e\necho 'hello $(world)'\nprintf '\\n'"
    command = build_remote_script_command(script)
    assert command.endswith("| base64 -d | sh")
    assert _decode_remote_command(command) == script


def test_apply_host_setup_on_outer_runs_all_steps() -> None:
    outer = _outer()
    apply_host_setup_on_outer(outer, install_gvisor_runtime=True, is_qemu_purge_enabled=True)
    expected_steps = build_host_setup_steps(install_gvisor_runtime=True, is_qemu_purge_enabled=True)
    recorded = _stub(outer).recorded_commands
    assert len(recorded) == len(expected_steps)
    # Each recorded command must decode back to the corresponding step's script.
    for command, step in zip(recorded, expected_steps, strict=True):
        assert _decode_remote_command(command) == step.script


def test_apply_host_setup_on_outer_omits_optional_steps_when_disabled() -> None:
    with_optional = _outer()
    apply_host_setup_on_outer(with_optional, install_gvisor_runtime=True, is_qemu_purge_enabled=True)
    without_optional = _outer()
    apply_host_setup_on_outer(without_optional, install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    # Enabling gVisor + qemu purge adds exactly two more commands.
    assert len(_stub(with_optional).recorded_commands) == len(_stub(without_optional).recorded_commands) + 2


def test_apply_host_setup_on_outer_raises_on_step_failure() -> None:
    outer = _outer(
        CommandResult(stdout="", stderr="", success=True),
        CommandResult(stdout="", stderr="", success=True),
        CommandResult(stdout="", stderr="", success=True),
        CommandResult(stdout="", stderr="E: version not found", success=False),
    )
    with pytest.raises(VpsProvisioningError, match="Docker"):
        apply_host_setup_on_outer(outer, install_gvisor_runtime=False, is_qemu_purge_enabled=False)
    assert len(_stub(outer).recorded_commands) == 4


def test_render_gvisor_binary_install_script_downloads_from_the_given_release_directory() -> None:
    # The default rendering fetches gVisor's own release bucket; a caller with a
    # mirrored copy of the same per-arch layout swaps only the base URL.
    assert f"storage.googleapis.com/gvisor/releases/release/{PINNED_GVISOR_RELEASE}/" in (
        PINNED_GVISOR_BINARY_INSTALL_SCRIPT
    )
    mirrored = render_gvisor_binary_install_script("https://mirror.example.test/artifacts/gvisor/20260601")
    assert 'URL="https://mirror.example.test/artifacts/gvisor/20260601/${ARCH}"' in mirrored
    assert "storage.googleapis.com" not in mirrored
    assert "sha512sum -c runsc.sha512" in mirrored
    assert "sha512sum -c containerd-shim-runsc-v1.sha512" in mirrored
