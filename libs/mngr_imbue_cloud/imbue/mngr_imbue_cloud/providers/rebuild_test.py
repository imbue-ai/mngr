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
from imbue.mngr_imbue_cloud.providers.rebuild import _build_slice_rebuild_config
from imbue.mngr_imbue_cloud.providers.rebuild import _slice_memory_mib_from_lease_attributes
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


def test_slice_rebuild_config_carries_every_vps_field_but_the_gvisor_knobs_and_layers_the_slice_coordinates() -> None:
    config = _account_config_with_non_default_knobs()
    slice_config = _build_slice_rebuild_config(config, box_public_address="203.0.113.7", slice_memory_mib=8192)
    assert slice_config.backend == "imbue_cloud_slice"
    assert slice_config.model_dump(include=set(_SLICE_DELEGATED_FIELDS)) == config.model_dump(
        include=set(_SLICE_DELEGATED_FIELDS)
    )
    # The slice VM's Docker is plain runc, so the account block's runsc settings must not reach the rebuilt container.
    assert slice_config.docker_runtime is None
    assert slice_config.install_gvisor_runtime is False
    assert slice_config.box_public_address == "203.0.113.7"
    assert slice_config.slice_memory_mib == 8192


def test_slice_memory_mib_from_lease_attributes_converts_the_stamped_memory_gb() -> None:
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": 8, "cpus": 2}) == 8192


def test_slice_memory_mib_from_lease_attributes_returns_none_for_missing_or_unusable_values() -> None:
    # A legacy row without the stamp, or a malformed value, must yield None (no
    # cap) rather than a bogus cap or a crash mid-create.
    assert _slice_memory_mib_from_lease_attributes({}) is None
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": "8"}) is None
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": True}) is None
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": 0}) is None
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": -4}) is None
