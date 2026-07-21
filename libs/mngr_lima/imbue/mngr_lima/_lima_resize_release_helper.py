"""End-to-end driver for the Lima resource-resize release test.

Invoked as a subprocess (via `runuser`) from `test_lima_resize_release.py`.
Lima refuses to run as root, so the release test installs Lima + qemu + a
non-root user as root, then re-enters this script under that user to drive
``LimaProviderInstance`` through create / resize / stop+start / verify /
destroy on a real Lima VM. It can also be run directly as a non-root user on
a developer machine with limactl installed.

Communicates via stdout: writes ``HELPER_RESULT: OK`` on success, otherwise
prints a Python traceback and exits non-zero.
"""

import os
import sys
import tempfile
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import HostResizeRequest
from imbue.mngr.interfaces.data_types import HostResizeValue
from imbue.mngr.interfaces.data_types import HostResourceLimits
from imbue.mngr.main import create_plugin_manager
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.instance import LimaProviderInstance


def _build_override_yaml() -> str:
    """Lima YAML override pinning the creation baseline the resize assertions start from.

    The VM driver is platform-appropriate: qemu+9p on Linux (modal release
    sandboxes have no KVM, so TCG with the cheap qemu64 CPU model), vz+virtiofs
    on macOS (no qemu install needed for a local developer run).
    """
    if sys.platform == "darwin":
        driver_block = "vmType: vz\nmountType: virtiofs"
    else:
        driver_block = "vmType: qemu\nmountType: 9p\ncpuType:\n  x86_64: qemu64"
    return f"""\
{driver_block}
cpus: 2
memory: 2GiB
disk: 10GiB
networks: []
"""


# The values the VM is created with (must match the override YAML above).
_CREATE_CPUS = 2.0
_CREATE_MEMORY_GIB = 2.0
# The values the resize moves to.
_RESIZED_CPUS = 3
_RESIZED_MEMORY_GIB = 3


def _build_provider(profile_dir: Path) -> tuple[LimaProviderInstance, ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="lima-resize-release")
    cg.__enter__()
    config = LimaProviderConfig(
        host_dir=Path("/mngr"),
        default_idle_timeout=3600,
        # Cold boot under TCG (no KVM in modal sandboxes) can take ~10-15 min.
        vm_start_timeout_seconds=1500.0,
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


def _require_limits(actual: HostResourceLimits | None, expected: HostResourceLimits, context: str) -> None:
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected}, got {actual}")


def main() -> int:
    if os.geteuid() == 0:
        print("HELPER_RESULT: FAIL (helper must run as non-root; Lima refuses root)", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="mngr-lima-resize-release-") as tmp:
        tmp_path = Path(tmp)
        profile_dir = tmp_path / "profile"
        profile_dir.mkdir()

        provider, cg = _build_provider(profile_dir)
        override_yaml_path = tmp_path / "vm-override.yaml"
        override_yaml_path.write_text(_build_override_yaml())

        created_limits = HostResourceLimits(cpu_count=_CREATE_CPUS, memory_gib=_CREATE_MEMORY_GIB)
        resized_limits = HostResourceLimits(cpu_count=float(_RESIZED_CPUS), memory_gib=float(_RESIZED_MEMORY_GIB))

        try:
            host = provider.create_host(
                name=HostName("release-resize"),
                build_args=(f"--file={override_yaml_path}",),
                start_args=("--timeout", "20m0s"),
            )
            if not isinstance(host, Host):
                raise AssertionError(f"create_host returned non-Host: {type(host).__name__}")

            try:
                # Create-time recording probes the booted values, so configured
                # and actual agree byte-exactly from the start.
                initial = provider.get_host_resource_limits(host.id)
                _require_limits(initial.configured, created_limits, "configured after create")
                _require_limits(initial.actual, created_limits, "actual after create")

                # A resize while running persists but does not touch the live VM:
                # the report shows the configured/actual discrepancy.
                resize_report = provider.resize_host(
                    host.id,
                    HostResizeRequest(
                        cpu_count=HostResizeValue(value=_RESIZED_CPUS),
                        memory_gib=HostResizeValue(value=_RESIZED_MEMORY_GIB),
                    ),
                )
                _require_limits(resize_report.configured, resized_limits, "configured after resize")
                _require_limits(resize_report.actual, created_limits, "actual after resize (still running old)")

                # Stopped: nothing to probe.
                provider.stop_host(host.id)
                stopped = provider.get_host_resource_limits(host.id)
                _require_limits(stopped.configured, resized_limits, "configured while stopped")
                if stopped.actual is not None:
                    raise AssertionError(f"actual should be absent while stopped, got {stopped.actual}")

                # start_host applies the configured values via limactl edit, so
                # the restarted VM boots with them and the discrepancy closes.
                restarted = provider.start_host(host.id)
                after_restart = provider.get_host_resource_limits(restarted.id)
                _require_limits(after_restart.configured, resized_limits, "configured after restart")
                _require_limits(after_restart.actual, resized_limits, "actual after restart")

                # The guest itself sees the new CPU count (memory is fuzzy from
                # inside the guest -- the kernel reserves some -- so only the CPU
                # count is asserted in-guest; the byte-exact memory check above
                # goes through limactl).
                nproc = restarted.execute_idempotent_command("nproc")
                if nproc.stdout.strip() != str(_RESIZED_CPUS):
                    raise AssertionError(f"Guest nproc={nproc.stdout.strip()!r}, expected {_RESIZED_CPUS}")
            finally:
                provider.destroy_host(host.id)
        finally:
            cg.__exit__(None, None, None)

    print("HELPER_RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
