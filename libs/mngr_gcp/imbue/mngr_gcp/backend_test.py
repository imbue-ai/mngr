"""Tests for GCP provider backend registration."""

import json
from datetime import datetime
from datetime import timezone

import pytest
from google.auth.credentials import AnonymousCredentials
from google.auth.credentials import Credentials
from google.cloud import compute_v1

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_gcp.backend import AGENT_METADATA_PREFIX
from imbue.mngr_gcp.backend import GCP_BACKEND_NAME
from imbue.mngr_gcp.backend import GcpProvider
from imbue.mngr_gcp.backend import GcpProviderBackend
from imbue.mngr_gcp.backend import HOST_STATE_METADATA_KEY
from imbue.mngr_gcp.backend import ParsedGcpBuildOptions
from imbue.mngr_gcp.backend import _GceMetadataHostStateStore
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.client import HOST_ID_METADATA_KEY
from imbue.mngr_gcp.client import HOST_NAME_METADATA_KEY
from imbue.mngr_gcp.client import ISOLATION_METADATA_KEY
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.errors import GcpCredentialsError
from imbue.mngr_gcp.testing import FakeInstancesClient
from imbue.mngr_gcp.testing import _StubbedGcpVpsClient
from imbue.mngr_vps.bare_realizer import BareRealizer
from imbue.mngr_vps.host_store import VpsHostConfig
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.testing import seed_stopped_host_record


class _StubAdcConfig(GcpProviderConfig):
    """GcpProviderConfig with ADC resolution stubbed for deterministic tests.

    ``build_provider_instance`` resolves credentials and the fallback project via
    ``get_credentials_and_resolved_project``, which calls ``google.auth.default()``.
    Stubbing it here keeps these tests independent of whatever gcloud / ADC state
    the test host happens to have configured.
    """

    stub_has_credentials: bool = True
    stub_resolved_project: str | None = None
    # Pin a zone so build_provider_instance takes the explicit-config branch and
    # never shells out to 'gcloud config get compute/zone', keeping these tests
    # hermetic (the gcloud probe and the unset-zone fallback are covered directly
    # in config_test.py).
    default_zone: str | None = "us-west1-a"

    def get_credentials_and_resolved_project(self) -> tuple[Credentials, str | None]:
        if not self.stub_has_credentials:
            raise GcpCredentialsError("GCP Application Default Credentials not configured (stub).")
        return AnonymousCredentials(), self.stub_resolved_project


def test_backend_name_and_config_class() -> None:
    assert GcpProviderBackend.get_name() == GCP_BACKEND_NAME
    assert GcpProviderBackend.get_config_class() is GcpProviderConfig


def test_backend_build_args_help_mentions_gcp_specific_args() -> None:
    """The build-args help is the only user-facing surface that describes
    GCE-specific build-arg overrides. It must mention the GCP-prefixed flags and
    call out that placement is a zone for GCP.
    """
    help_text = GcpProviderBackend.get_build_args_help()
    assert "GCE-specific" in help_text
    assert "--gcp-zone=ZONE" in help_text
    assert "--gcp-machine-type=TYPE" in help_text
    assert "zonal" in help_text
    # The per-host image override and its config-default source are both documented.
    assert "--gcp-image=IMAGE" in help_text
    assert "default_source_image" in help_text


def test_build_provider_instance_raises_provider_unavailable_without_credentials(
    temp_mngr_ctx: MngrContext,
) -> None:
    """No resolvable ADC surfaces as ProviderUnavailableError.

    Credentials failing means we never reached GCP, so the state is *unknown*
    (there may be running hosts we cannot see). ProviderUnavailableError -- not
    ProviderEmptyError -- is the correct signal: the shared discovery path
    surfaces it to the user instead of silently dropping the provider.
    """
    config = _StubAdcConfig(stub_has_credentials=False)
    with pytest.raises(ProviderUnavailableError):
        GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)


def test_build_provider_instance_raises_provider_unavailable_without_project_anywhere(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Credentials but no project (neither configured nor ADC-resolved) -> unavailable.

    Without a project we cannot enumerate the provider's hosts, so its state is
    unknown and it must be surfaced as unavailable rather than half-constructed.
    """
    config = _StubAdcConfig(stub_has_credentials=True, stub_resolved_project=None)
    with pytest.raises(ProviderUnavailableError):
        GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)


def test_generate_bootstrap_payload_is_gce_startup_script_not_cloud_init(
    temp_mngr_ctx: MngrContext,
) -> None:
    """GCP renders a GCE startup-script, since stock GCE images do not run cloud-init.

    This is what makes the default Debian 12 image work: the google-guest-agent
    executes the ``startup-script`` metadata on every image, unlike cloud-init's
    ``user-data`` which the stock GCE Debian images ignore.
    """
    config = _StubAdcConfig(stub_has_credentials=True, stub_resolved_project="p")
    provider = GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)
    assert isinstance(provider, GcpProvider)
    payload = provider._generate_bootstrap_payload(
        host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nk\n-----END OPENSSH PRIVATE KEY-----",
        host_public_key="ssh-ed25519 AAAAhost host",
        authorized_user_public_key="ssh-ed25519 AAAAaccess user@laptop",
    )
    assert payload.startswith("#!/bin/bash\n")
    assert "#cloud-config" not in payload
    # The provider key is injected straight into root (the guest agent races the
    # default-user copy on GCE).
    assert "'ssh-ed25519 AAAAaccess user@laptop'" in payload
    assert "touch /var/run/mngr-ready" in payload


def test_build_provider_instance_falls_back_to_adc_resolved_project(
    temp_mngr_ctx: MngrContext,
) -> None:
    """With no configured project_id, the ADC-resolved project is used.

    This is the gcloud-default fallback: a user who ran `gcloud config set
    project` (or set GOOGLE_CLOUD_PROJECT) can create without pinning project_id
    in the mngr config.
    """
    config = _StubAdcConfig(stub_has_credentials=True, stub_resolved_project="adc-resolved-project")
    provider = GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)
    assert isinstance(provider, GcpProvider)
    assert provider.gcp_client.project_id == "adc-resolved-project"


def test_build_provider_instance_prefers_configured_project_over_adc(
    temp_mngr_ctx: MngrContext,
) -> None:
    """An explicit project_id wins over whatever ADC resolved."""
    config = _StubAdcConfig(
        project_id="explicit-project",
        stub_has_credentials=True,
        stub_resolved_project="adc-resolved-project",
    )
    provider = GcpProviderBackend.build_provider_instance(ProviderInstanceName("gcp-test"), config, temp_mngr_ctx)
    assert isinstance(provider, GcpProvider)
    assert provider.gcp_client.project_id == "explicit-project"


class _FirewallStubClient(GcpVpsClient):
    """GcpVpsClient with firewall resolution stubbed, for hermetic create-hook tests.

    The real ``resolve_firewall`` makes a GCE API call, and the pre-create hook
    invokes it, so tests that exercise the hook stub it: ``resolve_firewall``
    returns the target tag, or (when ``stub_firewall_missing``) raises the same
    ``mngr gcp prepare`` MngrError the real method raises on a 404.
    """

    stub_firewall_missing: bool = False

    def resolve_firewall(self) -> str:
        if self.stub_firewall_missing:
            raise MngrError(
                f"GCP firewall rule {self.firewall_name!r} does not exist in project "
                f"{self.project_id!r}. Run `mngr gcp prepare --project {self.project_id}` once to create it."
            )
        return self.firewall_target_tag


def _build_provider(
    mngr_ctx: MngrContext, *, auto_shutdown_seconds: int | None, firewall_missing: bool = False
) -> GcpProvider:
    """Construct a GcpProvider with the given auto-shutdown and firewall settings.

    Uses anonymous credentials, a placeholder project, and a firewall-stubbed
    client: the create-hook and build-args tests that use this helper never make
    a real GCE API call.
    """
    config = GcpProviderConfig(
        backend=GCP_BACKEND_NAME,
        project_id="test-project",
        auto_shutdown_seconds=auto_shutdown_seconds,
    )
    client = _FirewallStubClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone=config.resolve_zone_and_region(None)[0],
        image=config.default_source_image,
        auto_shutdown_seconds=auto_shutdown_seconds,
        stub_firewall_missing=firewall_missing,
    )
    return GcpProvider(
        name=ProviderInstanceName("gcp-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        gcp_client=client,
        gcp_config=config,
    )


def test_validate_provider_args_under_pytest_raises_when_unset(temp_mngr_ctx: MngrContext) -> None:
    """The pre-create hook fires when auto_shutdown_seconds is None (the config default).

    Without it, a release test would launch instances with no self-delete safety
    net. The hook must abort the launch before any GCE API call.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=None)
    with pytest.raises(MngrError, match="auto_shutdown_seconds"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_accepts_positive(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    # auto_shutdown set and firewall present, so the hook passes.
    provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_raises_when_zero(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=0)
    with pytest.raises(MngrError, match="auto_shutdown_seconds"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_requires_firewall_rule(temp_mngr_ctx: MngrContext) -> None:
    """The pre-create hook fails fast with the `mngr gcp prepare` pointer when the rule is missing.

    This is the onboarding path: a first-time user who has not run prepare must
    get the actionable message before any provider write, not buried under a
    "Host creation failed, attempting cleanup..." line mid-create.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60, firewall_missing=True)
    with pytest.raises(MngrError, match="mngr gcp prepare"):
        provider._validate_provider_args_for_create()


# =============================================================================
# GCP build-args parser (--gcp-zone, --gcp-machine-type, --git-depth)
# =============================================================================


def test_parse_build_args_uses_defaults_when_none(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(None)
    # region holds the zone for GCP (base threads it to create_instance).
    assert parsed.region == "us-west1-a"
    assert parsed.plan == "e2-small"
    assert parsed.spot is False
    assert parsed.image is None
    assert parsed.git_depth is None
    assert parsed.docker_build_args == ()


def test_parse_build_args_extracts_gcp_knobs_plus_docker_passthrough(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(
        [
            "--gcp-zone=us-west1-b",
            "--gcp-machine-type=e2-medium",
            "--gcp-image=projects/my-proj/global/images/family/custom",
            "--gcp-spot",
            "--git-depth=1",
            "--file=Dockerfile",
            ".",
        ]
    )
    assert isinstance(parsed, ParsedGcpBuildOptions)
    assert parsed.region == "us-west1-b"
    assert parsed.plan == "e2-medium"
    assert parsed.spot is True
    assert parsed.image == "projects/my-proj/global/images/family/custom"
    assert parsed.git_depth == 1
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_rejects_gcp_spot_with_value(temp_mngr_ctx: MngrContext) -> None:
    """``--gcp-spot`` is presence-only; passing a value (e.g. ``--gcp-spot=true``) raises."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    with pytest.raises(MngrError, match="presence-only flag"):
        provider._parse_build_args(["--gcp-spot=true"])


def test_parse_build_args_rejects_unknown_gcp_flag(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    with pytest.raises(MngrError, match="Unknown gcp build arg"):
        provider._parse_build_args(["--gcp-bogus=foo"])


def test_parse_build_args_rejects_dropped_vps_prefix(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    with pytest.raises(MngrError, match="no longer supported"):
        provider._parse_build_args(["--vps-region=us-west1-a"])


# =============================================================================
# Offline discovery + the GCE-metadata-backed state store (stop/start lifecycle):
# instance lookup, agent/host-record mirror, discovery, offline reconstruction.
#
# These build a GcpProvider over a _StubbedGcpVpsClient whose FakeInstancesClient
# returns hand-built compute_v1.Instance objects; the provider normalizes them
# through the real list_instances path (labels -> "tags", metadata -> "metadata").
# The full host record lives in the ``mngr-host-state`` metadata value and one
# full agent JSON per agent under ``mngr-agent-<id>`` -- the GCP analog of the
# AWS/Azure object-storage state bucket, behind the same HostStateStore interface.
# =============================================================================


def _build_stubbed_provider(mngr_ctx: MngrContext) -> tuple[GcpProvider, FakeInstancesClient]:
    """Build a GcpProvider whose GCE client is a _StubbedGcpVpsClient over a FakeInstancesClient.

    Returns the provider and the fake so a test can seed ``list_result`` (the
    label/metadata-based lookups read it) and assert on recorded ``set_metadata``
    calls. The GCP analog of AWS's ``_build_stubbed_provider``.
    """
    config = GcpProviderConfig(backend=GCP_BACKEND_NAME, project_id="test-project", auto_shutdown_seconds=3600)
    instances = FakeInstancesClient()
    client = _StubbedGcpVpsClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone="us-west1-a",
        image=config.default_source_image,
        auto_shutdown_seconds=3600,
        stubbed_instances_client=instances,
    )
    provider = GcpProvider(
        name=ProviderInstanceName("gcp-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        gcp_client=client,
        gcp_config=config,
    )
    return provider, instances


def _instance(
    name: str,
    status: str,
    *,
    host_id: HostId | None = None,
    labels: dict[str, str] | None = None,
    metadata: dict[str, str] | None = None,
    nat_ip: str = "",
) -> compute_v1.Instance:
    """A compute_v1.Instance carrying GCE labels + metadata, as the GCE API returns it.

    GCP stores only ``mngr-provider`` as a label; the host id, host name, full
    host record, and per-agent records all live in metadata (surfaced in the
    normalized dict's "metadata"). ``host_id`` seeds the ``mngr-host-id`` metadata
    value the way production does, so this mirrors the real ``list_instances``
    normalization.
    """
    access_configs = [compute_v1.AccessConfig(nat_i_p=nat_ip)] if nat_ip else []
    full_metadata = dict(metadata or {})
    if host_id is not None:
        full_metadata[HOST_ID_METADATA_KEY] = str(host_id)
    return compute_v1.Instance(
        name=name,
        status=status,
        labels=labels or {},
        metadata=compute_v1.Metadata(items=[compute_v1.Items(key=k, value=v) for k, v in full_metadata.items()]),
        network_interfaces=[compute_v1.NetworkInterface(access_configs=access_configs)],
    )


def _agent_metadata_json(agent_id: AgentId, **fields: object) -> str:
    """Render a full agent record JSON, as the metadata store stores it under ``mngr-agent-<id>``."""
    return json.dumps({"id": str(agent_id), **fields})


def _host_state_metadata_json(host_id: HostId, host_name: str, *, vps_ip: str | None = None) -> str:
    """Render a full ``VpsHostRecord`` JSON, as the metadata store stores it under ``mngr-host-state``."""
    record = VpsHostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name=host_name,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            stop_reason=HostState.STOPPED.value,
        ),
        vps_ip=vps_ip,
    )
    return record.model_dump_json()


def test_find_instance_for_host_matches_by_host_id_metadata(temp_mngr_ctx: MngrContext) -> None:
    """``_find_instance_for_host`` resolves a (stopped) instance by its mngr-host-id metadata, no SSH."""
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    instances.list_result = [
        _instance(
            "i-match",
            "TERMINATED",
            host_id=host_id,
            labels={"mngr-provider": "gcp-test"},
        ),
        _instance(
            "i-other",
            "RUNNING",
            host_id=HostId.generate(),
            labels={"mngr-provider": "gcp-test"},
            nat_ip="10.0.0.9",
        ),
    ]
    found = provider._find_instance_for_host(host_id)
    assert found is not None
    assert found["id"] == "i-match"


def test_realizer_for_vps_ip_reads_bare_marker_from_gce_metadata(temp_mngr_ctx: MngrContext) -> None:
    """A GCE bare host (metadata ``mngr-isolation=none``) is probed with the BARE realizer.

    GCP stores the placement marker in metadata (not the normalized tag list), so the
    GcpProvider overrides ``_isolation_marker_for_instance`` to read it from there.
    Discovery then picks the bare realizer (port 22, no container probe) even though
    the provider config defaults to CONTAINER -- the fix for connecting to a bare host.
    """
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    instances.list_result = [
        _instance(
            "i-bare",
            "RUNNING",
            host_id=HostId.generate(),
            labels={"mngr-provider": "gcp-test"},
            metadata={ISOLATION_METADATA_KEY: "none"},
            nat_ip="10.0.0.42",
        ),
    ]
    realizer = provider._realizer_for_vps_ip("10.0.0.42")
    assert isinstance(realizer, BareRealizer)
    assert realizer.agent_endpoint("10.0.0.42").port == 22


def test_find_instance_for_host_returns_none_when_no_metadata_match(temp_mngr_ctx: MngrContext) -> None:
    """A host with no matching instance metadata resolves to None (after a cache-refresh retry).

    On a cache miss ``_find_instance_for_host`` drops the cache and re-lists once,
    so a just-created instance absent from a stale cache is still found. With a
    persistent miss the fake returns the same (non-matching) list both times.
    """
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    instances.list_result = [_instance("i-other", "RUNNING", host_id=HostId.generate(), nat_ip="10.0.0.9")]
    assert provider._find_instance_for_host(HostId.generate()) is None


def test_find_instance_for_host_refuses_duplicate_host_id_metadata(temp_mngr_ctx: MngrContext) -> None:
    """Two instances sharing a mngr-host-id are refused, not silently disambiguated.

    The metadata is account-writable, so a duplicate could otherwise steer
    ``mngr start`` (and the agent-metadata writes keyed off this lookup) onto the
    wrong instance. The lookup must raise rather than pick the first match.
    """
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    instances.list_result = [
        _instance("i-real", "TERMINATED", host_id=host_id),
        _instance("i-evil", "RUNNING", host_id=host_id, nat_ip="10.0.0.9"),
    ]
    with pytest.raises(MngrError, match="ambiguous"):
        provider._find_instance_for_host(host_id)


def test_rebind_known_hosts_pre_connect_uses_local_keypairs(temp_mngr_ctx: MngrContext) -> None:
    """The pre-connect known_hosts rebind pins mngr's own local host keys, not metadata.

    On resume the new IP is added to known_hosts *before* the first SSH. Sourcing
    the host keys from the locally held provider keypairs (injected into the box
    at create) -- rather than account-writable instance metadata -- is what
    prevents an attacker who can edit metadata from substituting their own host
    key and MITMing the resumed session.
    """
    provider, _instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    new_ip = "203.0.113.50"
    expected_vps_key = provider._get_vps_host_keypair(host_id)[1]
    expected_container_key = provider._get_container_host_keypair(host_id)[1]

    provider._rebind_known_hosts_pre_connect(host_id, new_ip)

    vps_known_hosts = provider._vps_known_hosts_path().read_text()
    container_known_hosts = provider._container_known_hosts_path().read_text()
    assert new_ip in vps_known_hosts and expected_vps_key in vps_known_hosts
    assert new_ip in container_known_hosts and expected_container_key in container_known_hosts


def test_state_store_is_gce_metadata_backed(temp_mngr_ctx: MngrContext) -> None:
    """GCP has no object-storage bucket; its ``_state_store`` is the GCE-metadata store.

    This is the GCP arm of the uniform ``HostStateStore`` interface the AWS/Azure
    object-storage buckets also implement -- the offline read/write paths on the
    shared base dispatch through it identically.
    """
    provider, _instances = _build_stubbed_provider(temp_mngr_ctx)
    assert isinstance(provider._state_store, _GceMetadataHostStateStore)


def test_persist_agent_data_writes_full_agent_json_to_metadata(temp_mngr_ctx: MngrContext) -> None:
    """persist_agent_data finds the instance by host label and writes the full agent JSON under one key.

    Exercises the stopped-host path (the on-volume base write is unavailable, so
    only the GCE metadata is written); the seeded ``vps_ip=None`` record makes
    ``super().persist_agent_data`` short-circuit with ``HostNotFoundError``. One
    ``set_instance_metadata`` round-trip writes the full agent record JSON under
    ``mngr-agent-<id>`` -- uniform with the bucket's per-agent object.
    """
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)
    listed = _instance("i-1", "TERMINATED", host_id=host_id)
    instances.list_result = [listed]
    # set_metadata reads the live instance first (whole-object read-modify-write).
    instances.get_result = listed
    provider.persist_agent_data(
        host_id,
        {"id": str(agent_id), "name": "a1", "type": "command", "labels": {"env": "prod"}},
    )
    assert len(instances.set_metadata_calls) == 1
    written = {item.key: item.value for item in instances.set_metadata_calls[0].items}
    stored = json.loads(written[f"{AGENT_METADATA_PREFIX}{agent_id}"])
    assert stored == {"id": str(agent_id), "name": "a1", "type": "command", "labels": {"env": "prod"}}


def test_remove_persisted_agent_data_deletes_the_agent_key(temp_mngr_ctx: MngrContext) -> None:
    """Removing an agent drops its single ``mngr-agent-<id>`` metadata key (idempotent)."""
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)
    listed = _instance(
        "i-1",
        "TERMINATED",
        host_id=host_id,
        metadata={f"{AGENT_METADATA_PREFIX}{agent_id}": _agent_metadata_json(agent_id, name="a1")},
    )
    instances.list_result = [listed]
    instances.get_result = listed
    provider.remove_persisted_agent_data(host_id, agent_id)
    assert len(instances.set_metadata_calls) == 1
    written = {item.key: item.value for item in instances.set_metadata_calls[0].items}
    assert f"{AGENT_METADATA_PREFIX}{agent_id}" not in written


def test_persist_host_record_externally_writes_full_record_to_metadata(temp_mngr_ctx: MngrContext) -> None:
    """The full host record is mirrored to the ``mngr-host-state`` metadata value (bucket parity)."""
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    listed = _instance("i-1", "TERMINATED", host_id=host_id)
    instances.list_result = [listed]
    instances.get_result = listed
    record = VpsHostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="myhost",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            stop_reason=HostState.STOPPED.value,
        ),
        vps_ip="203.0.113.7",
    )
    provider._persist_host_record_externally(record)
    assert len(instances.set_metadata_calls) == 1
    written = {item.key: item.value for item in instances.set_metadata_calls[0].items}
    round_tripped = VpsHostRecord.model_validate_json(written[HOST_STATE_METADATA_KEY])
    assert round_tripped.certified_host_data.host_name == "myhost"
    assert round_tripped.vps_ip == "203.0.113.7"


def test_remirror_host_name_restamps_identity_metadata(temp_mngr_ctx: MngrContext) -> None:
    """A rename re-stamps ``mngr-host-name`` (read by offline discovery) to ``mngr-<newname>``."""
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    listed = _instance(
        "i-1",
        "TERMINATED",
        host_id=host_id,
        metadata={HOST_NAME_METADATA_KEY: "mngr-oldname"},
    )
    instances.get_result = listed
    record = VpsHostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="newname",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            stop_reason=HostState.STOPPED.value,
        ),
        config=VpsHostConfig(vps_instance_id=VpsInstanceId("i-1"), region="us-west1-a", plan="e2-medium"),
    )
    provider._remirror_host_name(record, HostName("newname"))
    assert len(instances.set_metadata_calls) == 1
    written = {item.key: item.value for item in instances.set_metadata_calls[0].items}
    assert written[HOST_NAME_METADATA_KEY] == "mngr-newname"


def test_remirror_host_name_no_op_without_config(temp_mngr_ctx: MngrContext) -> None:
    """No config means no instance id to address, so the re-stamp is a no-op (no API call)."""
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    record = VpsHostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(HostId.generate()),
            host_name="newname",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            stop_reason=HostState.STOPPED.value,
        ),
    )
    provider._remirror_host_name(record, HostName("newname"))
    assert instances.set_metadata_calls == []


def test_list_persisted_agent_data_for_host_reads_metadata(temp_mngr_ctx: MngrContext) -> None:
    """list_persisted_agent_data_for_host reads the full agent JSON from metadata (stopped host)."""
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    instances.list_result = [
        _instance(
            "i-1",
            "TERMINATED",
            host_id=host_id,
            metadata={
                f"{AGENT_METADATA_PREFIX}{agent_id}": _agent_metadata_json(
                    agent_id, name="a1", type="command", labels={"env": "prod"}
                )
            },
        )
    ]
    agents = provider.list_persisted_agent_data_for_host(host_id)
    assert len(agents) == 1
    assert agents[0]["id"] == str(agent_id)
    assert agents[0]["name"] == "a1"
    assert agents[0]["type"] == "command"
    assert agents[0]["labels"] == {"env": "prod"}


def test_discover_hosts_and_agents_surfaces_terminated_host_from_metadata(temp_mngr_ctx: MngrContext) -> None:
    """A TERMINATED instance (no external IP) is surfaced as a STOPPED host with its agents from metadata."""
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    instances.list_result = [
        _instance(
            "i-1",
            "TERMINATED",
            host_id=host_id,
            labels={"mngr-provider": "gcp-test"},
            metadata={
                HOST_NAME_METADATA_KEY: "mngr-myhost",
                f"{AGENT_METADATA_PREFIX}{agent_id}": _agent_metadata_json(agent_id, name="a1", type="command"),
            },
        )
    ]
    with ConcurrencyGroup(name="test") as cg:
        result = provider.discover_hosts_and_agents(cg)
    hosts = list(result.keys())
    assert len(hosts) == 1
    assert hosts[0].host_id == host_id
    assert str(hosts[0].host_name) == "myhost"
    assert hosts[0].host_state == HostState.STOPPED
    agents = result[hosts[0]]
    assert len(agents) == 1
    assert agents[0].agent_id == agent_id
    assert str(agents[0].agent_name) == "a1"


def test_discover_hosts_and_agents_surfaces_stopping_host_during_transition(temp_mngr_ctx: MngrContext) -> None:
    """A still-STOPPING instance (OS down) is surfaced from metadata so it doesn't vanish mid-stop.

    ``_HOST_DOWN_STATES`` includes STOPPING so a host stays discoverable across
    the stop transition before it reaches the terminal TERMINATED, keeping
    resolve-by-name stable for ``mngr start``.
    """
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    instances.list_result = [
        _instance(
            "i-1",
            "STOPPING",
            host_id=host_id,
            labels={"mngr-provider": "gcp-test"},
            metadata={
                HOST_NAME_METADATA_KEY: "mngr-myhost",
                f"{AGENT_METADATA_PREFIX}{agent_id}": _agent_metadata_json(agent_id, name="a1"),
            },
        )
    ]
    with ConcurrencyGroup(name="test") as cg:
        result = provider.discover_hosts_and_agents(cg)
    hosts = {host.host_id: host for host in result}
    assert host_id in hosts
    assert hosts[host_id].host_state == HostState.STOPPED
    assert [a.agent_id for a in result[hosts[host_id]]] == [agent_id]


def test_discover_hosts_and_agents_skips_instance_with_absent_host_id_metadata(temp_mngr_ctx: MngrContext) -> None:
    """An instance with no mngr-host-id metadata is skipped; a well-formed stopped host still surfaces.

    A missing host-id yields no DiscoveredHost for that instance; the
    offline-discovery loop must skip it and still surface the well-formed stopped
    host, rather than letting one bad instance take down the whole sweep.
    """
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    good_host_id = HostId.generate()
    instances.list_result = [
        _instance("i-bad", "TERMINATED", labels={"mngr-provider": "gcp-test"}),
        _instance(
            "i-good",
            "TERMINATED",
            host_id=good_host_id,
            labels={"mngr-provider": "gcp-test"},
            metadata={HOST_NAME_METADATA_KEY: "mngr-goodhost"},
        ),
    ]
    with ConcurrencyGroup(name="test") as cg:
        result = provider.discover_hosts_and_agents(cg)
    host_ids = {host.host_id for host in result}
    assert good_host_id in host_ids
    assert len(host_ids) == 1


def test_to_offline_host_reconstructs_full_record_from_metadata(temp_mngr_ctx: MngrContext) -> None:
    """to_offline_host rebuilds the full STOPPED host record from ``mngr-host-state`` when SSH can't reach it.

    Unlike the previous lossy label reconstruction, the full ``VpsHostRecord``
    (name, IP, ...) is read back from the metadata state record -- parity with the
    AWS/Azure bucket's ``host_state.json``.
    """
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    instances.list_result = [
        _instance(
            "i-1",
            "TERMINATED",
            host_id=host_id,
            metadata={HOST_STATE_METADATA_KEY: _host_state_metadata_json(host_id, "myhost", vps_ip="203.0.113.7")},
        )
    ]
    offline = provider.to_offline_host(host_id)
    assert offline.id == host_id
    assert str(offline.get_certified_data().host_name) == "myhost"
    assert offline.get_state() == HostState.STOPPED
    created_at = offline.get_certified_data().created_at
    assert (created_at.year, created_at.month, created_at.day) == (2026, 1, 1)


def test_to_offline_host_raises_when_no_host_state_metadata(temp_mngr_ctx: MngrContext) -> None:
    """With the instance present but no ``mngr-host-state`` record, the offline read raises HostNotFound.

    This matches the AWS/Azure bucket: a host the store has never recorded cannot
    be reconstructed, rather than fabricating a lossy stand-in.
    """
    provider, instances = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    instances.list_result = [_instance("i-1", "TERMINATED", host_id=host_id)]
    with pytest.raises(HostNotFoundError):
        provider.to_offline_host(host_id)
