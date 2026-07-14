"""Behavior-guard driver for the Lima host-start readiness race.

Invoked as a subprocess (via `runuser`) from `test_lima_host_start_race.py`.
Lima refuses to run as root, so the release test installs Lima + qemu + a
non-root user as root, then re-enters this script under that user.

This test pins the two Lima behaviors the boot-in-flight fix rests on (see
``is_limactl_start_in_flight_for_instance`` in ``limactl.py`` and the
STARTING classification in ``instance.py``). If a future Lima release changes
either, this test fails so we catch it at the version bump rather than in
production:

1. Window coverage + detection: while a stopped VM boots, there is a real
   window where `limactl list` reports the instance ``Running`` but the guest
   sshd is not yet reachable -- and for every such observed sample, the
   boot-in-flight detector sees the in-flight `limactl start`. This is the
   invariant the classification depends on: whenever the racy "Running but
   unreachable" state is observable, the detector catches it.
2. Ready-on-exit: when the `limactl start` process exits 0, the guest sshd
   answers essentially immediately -- i.e. the start process's lifetime covers
   the whole readiness window.

Communicates via stdout: writes ``HELPER_RESULT: OK`` on success, otherwise
propagates a Python traceback and exits non-zero.
"""

import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import paramiko
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.hosts.host import Host
from imbue.mngr.main import create_plugin_manager
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.instance import LimaProviderInstance
from imbue.mngr_lima.limactl import is_limactl_start_in_flight_for_instance
from imbue.mngr_lima.limactl import list_running_process_argvs

# Forces qemu+TCG (no KVM in modal sandboxes) and keeps the VM cheap.
_QEMU64_OVERRIDE_YAML = """\
vmType: qemu
cpus: 2
memory: 2GiB
disk: 10GiB
mountType: 9p
cpuType:
  x86_64: qemu64
networks: []
"""

_SSH_HOST = "127.0.0.1"
_SSH_PROBE_TIMEOUT_SECONDS = 2.0
# Safety cap so the sampling loop cannot spin forever if `limactl start` never exits;
# at ~1 list+probe per iteration this is far more than any realistic TCG boot produces.
_MAX_BOOT_SAMPLES = 5000
# `limactl start` exits at ssh-ready, but allow a few immediate re-probes to absorb a
# sub-second teardown/reconnect gap without flaking. Each probe self-paces via its timeout.
_READY_ON_EXIT_PROBE_ATTEMPTS = 15
# Generous per-process budget for the sampled restart under TCG (software emulation).
_START_TIMEOUT_SECONDS = 1500.0


class _BootSample(FrozenModel):
    """One observation taken while a stopped VM boots back to readiness."""

    lima_status: str = Field(description="Instance status reported by `limactl list` at sample time")
    is_sshd_ready: bool = Field(description="Whether an SSH transport handshake to the guest succeeded")
    is_start_detected: bool = Field(description="Whether the boot-in-flight detector saw the `limactl start`")


def _build_provider(profile_dir: Path) -> tuple[LimaProviderInstance, ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="lima-host-start-race")
    cg.__enter__()
    config = LimaProviderConfig(
        host_dir=Path("/mngr"),
        default_idle_timeout=3600,
        # Cold boot of a Debian cloud image under TCG is minutes; the default is for KVM.
        vm_start_timeout_seconds=_START_TIMEOUT_SECONDS,
    )
    pm = create_plugin_manager()
    mngr_config = MngrConfig.model_construct(
        prefix="mngr-",
        default_host_dir=Path("/mngr"),
        agent_types={},
        providers={"lima": config},
        plugins={},
    )
    ctx = make_mngr_ctx(mngr_config, pm, profile_dir, concurrency_group=cg)
    provider = LimaProviderInstance(
        name=ProviderInstanceName("lima"),
        host_dir=Path("/mngr"),
        mngr_ctx=ctx,
        config=config,
    )
    return provider, cg


def _instance_status_and_port(cg: ConcurrencyGroup, instance_name: str) -> tuple[str, int]:
    """Return the (status, forwarded-ssh-port) for ``instance_name`` from `limactl list --json`.

    Status is "" and port is 0 if the instance is absent or the port is not yet assigned.
    """
    result = cg.run_process_to_completion(["limactl", "list", "--json"], timeout=30.0)
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        instance = json.loads(line)
        if instance.get("name") == instance_name:
            return str(instance.get("status") or ""), int(instance.get("sshLocalPort") or 0)
    return "", 0


def _is_sshd_handshake_ready(hostname: str, port: int) -> bool:
    """Whether a single full SSH transport handshake to hostname:port succeeds right now."""
    if port <= 0:
        return False
    transport = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(_SSH_PROBE_TIMEOUT_SECONDS)
        sock.connect((hostname, port))
        transport = paramiko.Transport(sock)
        transport.connect()
        return True
    except (OSError, paramiko.SSHException, EOFError):
        return False
    finally:
        if transport is not None:
            try:
                transport.close()
            except (OSError, paramiko.SSHException):
                pass
        else:
            sock.close()


def _sample_boot(cg: ConcurrencyGroup, instance_name: str, start_proc: RunningProcess) -> list[_BootSample]:
    """Sample (status, sshd-ready, detected) repeatedly while the `limactl start` is running.

    Each iteration's `limactl list` + SSH probe naturally paces the loop (no fixed sleep).
    """
    samples: list[_BootSample] = []
    while not start_proc.is_finished() and len(samples) < _MAX_BOOT_SAMPLES:
        status, port = _instance_status_and_port(cg, instance_name)
        is_sshd_ready = _is_sshd_handshake_ready(_SSH_HOST, port)
        is_start_detected = is_limactl_start_in_flight_for_instance(instance_name, list_running_process_argvs())
        samples.append(
            _BootSample(lima_status=status, is_sshd_ready=is_sshd_ready, is_start_detected=is_start_detected)
        )
    return samples


def _probe_sshd_ready_after_exit(cg: ConcurrencyGroup, instance_name: str) -> bool:
    """After `limactl start` exits, whether sshd answers within a few immediate re-probes."""
    _status, port = _instance_status_and_port(cg, instance_name)
    for _attempt in range(_READY_ON_EXIT_PROBE_ATTEMPTS):
        if _is_sshd_handshake_ready(_SSH_HOST, port):
            return True
    return False


def _assert_boot_behavior(samples: list[_BootSample], is_ready_after_exit: bool) -> None:
    """Assert the two load-bearing Lima behaviors hold across the sampled boot."""
    # Window coverage: the race window must actually be observed, or the test proves nothing.
    window_samples = [s for s in samples if s.lima_status == "Running" and not s.is_sshd_ready]
    if not window_samples:
        raise AssertionError(
            "No sample observed the race window (status=Running while sshd not yet reachable); "
            f"the guard is vacuous. Collected {len(samples)} samples: "
            f"{[s.model_dump() for s in samples]}"
        )

    # Detection invariant: every observed racy sample was caught by the boot-in-flight detector.
    undetected = [s for s in window_samples if not s.is_start_detected]
    if undetected:
        raise AssertionError(
            f"Boot-in-flight detector missed the race window in {len(undetected)} of "
            f"{len(window_samples)} racy samples -- a lima behavior change may have broken detection: "
            f"{[s.model_dump() for s in undetected]}"
        )

    # Ready-on-exit: the start process's lifetime covers the whole window.
    if not is_ready_after_exit:
        raise AssertionError("`limactl start` exited but sshd did not answer within the ready-on-exit probe budget")


def main() -> int:
    if os.geteuid() == 0:
        print("HELPER_RESULT: FAIL (helper must run as non-root; Lima refuses root)", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="mngr-lima-race-") as tmp:
        tmp_path = Path(tmp)
        profile_dir = tmp_path / "profile"
        profile_dir.mkdir()

        provider, cg = _build_provider(profile_dir)
        override_yaml_path = tmp_path / "qemu64-override.yaml"
        override_yaml_path.write_text(_QEMU64_OVERRIDE_YAML)

        host: Host | None = None
        try:
            # Create + boot a VM once (blocks until ssh-ready), then stop it so we can drive a
            # controlled restart whose boot window we sample.
            host = provider.create_host(
                name=HostName("release-host-start-race"),
                build_args=(f"--file={override_yaml_path}",),
                start_args=("--timeout", "20m0s"),
            )
            if not isinstance(host, Host):
                raise AssertionError(f"create_host returned non-Host: {type(host).__name__}")

            record = provider._host_store.read_host_record(host.id, use_cache=False)
            if record is None or record.config is None:
                raise AssertionError("HostRecord not persisted after create_host")
            instance_name = record.config.instance_name

            provider.stop_host(host)

            # Drive the restart ourselves (mngr's existing-instance start shape) so we can watch
            # the boot window; is_checked_by_group=False because we inspect the exit code by hand.
            start_proc = cg.run_process_in_background(
                ["limactl", "--log-level=info", "start", instance_name],
                is_checked_by_group=False,
                timeout=_START_TIMEOUT_SECONDS,
            )
            samples = _sample_boot(cg, instance_name, start_proc)

            start_returncode = start_proc.wait(timeout=60.0)
            if start_returncode != 0:
                raise AssertionError(
                    f"`limactl start {instance_name}` exited {start_returncode}; boot did not complete cleanly. "
                    f"stdout/stderr:\n{start_proc.read_stdout()}"
                )

            is_ready_after_exit = _probe_sshd_ready_after_exit(cg, instance_name)
            _assert_boot_behavior(samples, is_ready_after_exit)

        finally:
            try:
                if host is not None:
                    provider.destroy_host(host.id)
            finally:
                cg.__exit__(None, None, None)

    print("HELPER_RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
