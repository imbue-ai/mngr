"""Unit tests for the slow-path rebuild provider/config builders."""

from pathlib import Path

from imbue.imbue_common.primitives import PositiveFloat
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import IdleMode
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.providers.rebuild import _DELEGATED_FIELDS
from imbue.mngr_imbue_cloud.providers.rebuild import _SLICE_DELEGATED_FIELDS
from imbue.mngr_imbue_cloud.providers.rebuild import _build_delegated_vps_config
from imbue.mngr_imbue_cloud.providers.rebuild import build_slice_rebuild_config
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GUEST_RAM_HOLDBACK_MIB
from imbue.mngr_imbue_cloud.wire_types import LeaseResult
from imbue.mngr_vps.primitives import IsolationMode

_ACCOUNT = ImbueCloudAccount("a@b.com")


def _account_config_with_non_default_knobs() -> ImbueCloudProviderConfig:
    """An account block like the one minds writes, with every delegated field set off its default.

    A builder that drops a field can only be caught when the field's forwarded
    value differs from the default the delegated config would have taken on its
    own, so this leaves none of them at their default -- see the assertion
    below, which fails when a newly added ``VpsProviderConfig`` field is not
    given a value here.
    """
    config = ImbueCloudProviderConfig(
        account=_ACCOUNT,
        # The layout + hardening knobs minds writes into the per-account block.
        host_dir=Path("/home/user/.mngr"),
        volume_home_path=Path("/home/user"),
        host_log_dir=Path("/var/log/mngr"),
        docker_runtime="runsc",
        install_gvisor_runtime=True,
        default_start_args=("--workdir=/", "--security-opt=no-new-privileges"),
        # The rest of the VpsProviderConfig surface, off its defaults.
        isolation=IsolationMode.NONE,
        container_ssh_port=2223,
        btrfs_mount_path=Path("/mngr-btrfs-alt"),
        btrfs_loop_file_path=Path("/var/lib/mngr-btrfs-alt.img"),
        outer_disk_reserved_gb=30,
        default_image="debian:trixie-slim",
        default_region="lhr",
        default_idle_timeout=1234,
        default_idle_mode=IdleMode.DISABLED,
        default_activity_sources=(ActivitySource.SSH,),
        auto_shutdown_seconds=3600,
        builder=DockerBuilder.DEPOT,
        ssh_connect_timeout=45,
        instance_boot_timeout=900,
        docker_install_timeout=420,
        # The ProviderInstanceConfig half of the surface. The discovery timeouts
        # must stay ordered below discovery_error_timeout_seconds.
        plugin="imbue_cloud",
        is_enabled=True,
        destroyed_host_persisted_seconds=111.0,
        min_online_host_age_seconds=222.0,
        discovery_poll_interval_seconds=PositiveFloat(7.0),
        discovery_warn_seconds=PositiveFloat(100.0),
        host_discovery_timeout_seconds=PositiveFloat(110.0),
        agent_discovery_timeout_seconds=PositiveFloat(120.0),
        discovery_error_timeout_seconds=PositiveFloat(600.0),
    )
    configured = config.model_dump(include=set(_DELEGATED_FIELDS))
    defaults = ImbueCloudProviderConfig(account=_ACCOUNT).model_dump(include=set(_DELEGATED_FIELDS))
    left_at_default = sorted(name for name, value in configured.items() if value == defaults[name])
    assert not left_at_default, (
        f"give these fields a non-default value so a dropped field is visible: {left_at_default}"
    )
    return config


def test_delegated_vps_config_carries_every_vps_field_of_the_account_config() -> None:
    """The slow-path VPS rebuild must carve and run exactly as a provider under the account config would.

    Checked over the whole VpsProviderConfig surface so a newly added field
    cannot be dropped silently.
    """
    config = _account_config_with_non_default_knobs()
    vps_config = _build_delegated_vps_config(config)
    assert vps_config.backend == "vps_docker"
    assert vps_config.model_dump(include=set(_DELEGATED_FIELDS)) == config.model_dump(include=set(_DELEGATED_FIELDS))
    assert vps_config.volume_home_path == Path("/home/user")
    assert vps_config.docker_runtime == "runsc"


def test_slice_rebuild_config_carries_every_vps_field_but_the_runtime_knobs_and_layers_the_slice_coordinates() -> None:
    """The slow-path slice rebuild must carve and run exactly as a provider under the account config would.

    Checked over the whole VpsProviderConfig surface (minus the knobs that
    follow the slice's generation) so a newly added field cannot be dropped
    silently.
    """
    config = _account_config_with_non_default_knobs()
    slice_config = build_slice_rebuild_config(config, _lease(box_generation=1))
    assert slice_config.backend == "imbue_cloud_slice"
    assert slice_config.model_dump(include=set(_SLICE_DELEGATED_FIELDS)) == config.model_dump(
        include=set(_SLICE_DELEGATED_FIELDS)
    )
    assert slice_config.volume_home_path == Path("/home/user")
    # The runsc host setup never runs on a slice VM, whatever the generation.
    assert slice_config.install_gvisor_runtime is False
    assert slice_config.box_public_address == "51.81.208.81"


def _lease(box_generation: int, memory_units: int | None = 8) -> LeaseResult:
    body: dict[str, object] = {
        "host_db_id": "11111111-1111-1111-1111-111111111111",
        "vps_address": "51.81.208.81",
        "ssh_port": 22004,
        "ssh_user": "root",
        "container_ssh_port": 22005,
        "agent_id": "agent-" + "a" * 32,
        "host_id": "host-" + "b" * 32,
        "host_name": "my-workspace",
        "attributes": {"memory_gb": 8, "cpus": 2},
        "box_generation": box_generation,
    }
    if memory_units is not None:
        body["memory_units"] = memory_units
    return LeaseResult.model_validate(body)


def _runsc_account_config() -> ImbueCloudProviderConfig:
    """The per-account block minds bootstrap writes: runsc plus the hardening start args."""
    return ImbueCloudProviderConfig(
        account=ImbueCloudAccount("a@b.com"),
        docker_runtime="runsc",
        default_start_args=("--workdir=/", "--security-opt=no-new-privileges"),
    )


def test_slice_rebuild_config_runs_a_gen2_container_under_the_account_runtime_with_tmpfs() -> None:
    slice_config = build_slice_rebuild_config(_runsc_account_config(), _lease(box_generation=2))
    # The gen-2 guest image ships runsc; the rebuilt container gets the same
    # runtime + hardening args + tmpfs mounts the bake creates it with.
    assert slice_config.docker_runtime == "runsc"
    assert slice_config.default_start_args == (
        "--workdir=/",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/tmp",
    )
    assert slice_config.box_generation == 2
    # The cap is sized from the guest's RAM exactly as the bake sizes it: a gen-2
    # guest boots with its units minus the holdback, so the rebuilt container is
    # capped like the original (and like the guest's own reconcile oneshot caps it).
    assert slice_config.slice_memory_mib == 8 * 1024 - GUEST_RAM_HOLDBACK_MIB
    assert slice_config.box_public_address == "51.81.208.81"


def test_slice_rebuild_config_keeps_a_gen1_container_on_plain_runc() -> None:
    slice_config = build_slice_rebuild_config(_runsc_account_config(), _lease(box_generation=1))
    # A lima guest has no runsc: forcing the runtime would fail every rebuild
    # with docker's "unknown runtime".
    assert slice_config.docker_runtime is None
    assert slice_config.default_start_args == ()
    assert slice_config.box_generation == 1
    # A lima guest gets its full advertised RAM, so its cap is sized from all of it.
    assert slice_config.slice_memory_mib == 8 * 1024


def test_slice_rebuild_config_without_a_machine_size_has_no_memory_cap() -> None:
    # A connector too old to serve the sizing columns omits memory_units; the
    # bake-stamped memory_gb attribute is deliberately NOT consulted, so the
    # rebuilt container gets no cap rather than a guessed one.
    slice_config = build_slice_rebuild_config(_runsc_account_config(), _lease(box_generation=2, memory_units=None))
    assert slice_config.slice_memory_mib is None
