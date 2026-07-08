"""Tests for AWS provider backend registration."""

from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone

import boto3
import pytest
from botocore.stub import Stubber

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws.backend import AWS_BACKEND_NAME
from imbue.mngr_aws.backend import AwsProvider
from imbue.mngr_aws.backend import AwsProviderBackend
from imbue.mngr_aws.backend import ParsedAwsBuildOptions
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.testing import _StubbedAwsVpsClient
from imbue.mngr_aws.testing import clear_aws_env
from imbue.mngr_vps.bare_realizer import BareRealizer
from imbue.mngr_vps.docker_realizer import DockerRealizer
from imbue.mngr_vps.host_state_store import BucketHostStateStore
from imbue.mngr_vps.host_store import VpsHostConfig
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.primitives import ISOLATION_TAG_KEY
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.testing import seed_stopped_host_record


def test_backend_build_args_help_mentions_aws_specific_args() -> None:
    """The build-args help is consumed by ``mngr help create`` and is the only
    user-facing surface that describes EC2-specific build-arg overrides. It
    must mention the AWS-specific flags (--aws-region, --aws-instance-type,
    --aws-ami) and the fact that the AMI override falls back to the provider
    config's default_ami_id when omitted.
    """
    help_text = AwsProviderBackend.get_build_args_help()
    assert "EC2-specific" in help_text, "help should call out that these args are EC2-specific"
    assert "--aws-region=REGION" in help_text
    assert "--aws-instance-type=TYPE" in help_text
    assert "--aws-ami=AMI-ID" in help_text
    assert "default_ami_id" in help_text


def _build_provider(mngr_ctx: MngrContext, *, auto_shutdown_seconds: int | None) -> AwsProvider:
    """Construct an AwsProvider with the given auto-shutdown setting.

    Uses a plain boto3 Session and a placeholder AMI: this helper is only
    used by tests that exercise the pytest-detection guard, which fires
    before any EC2 API call, so the session/AMI are never touched.
    """
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_ami_id="ami-placeholder",
        auto_shutdown_seconds=auto_shutdown_seconds,
    )
    client = AwsVpsClient(
        session=boto3.Session(region_name=config.default_region),
        region=config.default_region,
        ami_id="ami-placeholder",
        security_group=ExistingSecurityGroup(id="sg-placeholder"),
    )
    return AwsProvider(
        name=ProviderInstanceName("aws-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        aws_client=client,
        aws_config=config,
    )


def _build_stubbed_provider(mngr_ctx: MngrContext) -> tuple[AwsProvider, Stubber]:
    """Build an AwsProvider whose EC2 client is a botocore Stubber.

    Used by the ``_find_instance_for_host`` tests, which need to script a
    ``describe_instances`` response (the tag-based lookup that resolves a
    stopped instance) without any real AWS call.
    """
    config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_ami_id="ami-x", auto_shutdown_seconds=3600)
    session = boto3.Session(aws_access_key_id="AKIATEST", aws_secret_access_key="secret", region_name="us-east-1")
    ec2 = session.client("ec2", region_name="us-east-1")
    stubber = Stubber(ec2)
    client = _StubbedAwsVpsClient(
        session=session,
        region="us-east-1",
        ami_id="ami-x",
        security_group=ExistingSecurityGroup(id="sg-x"),
        stubbed_ec2_client=ec2,
    )
    provider = AwsProvider(
        name=ProviderInstanceName("aws-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        aws_client=client,
        aws_config=config,
    )
    return provider, stubber


def _describe_instances_response(instances: list[dict]) -> dict:
    return {"Reservations": [{"Instances": instances}]}


def test_find_instance_for_host_matches_by_host_id_tag(temp_mngr_ctx: MngrContext) -> None:
    """``_find_instance_for_host`` resolves a (stopped) instance by its mngr-host-id tag, no SSH."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                {
                    "InstanceId": "i-match",
                    "State": {"Name": "stopped"},
                    "Tags": [
                        {"Key": "mngr-host-id", "Value": str(host_id)},
                        {"Key": "mngr-provider", "Value": "aws-test"},
                    ],
                },
                {
                    "InstanceId": "i-other",
                    "State": {"Name": "running"},
                    "Tags": [
                        {"Key": "mngr-host-id", "Value": str(HostId.generate())},
                        {"Key": "mngr-provider", "Value": "aws-test"},
                    ],
                },
            ]
        ),
    )
    stubber.activate()
    try:
        found = provider._find_instance_for_host(host_id)
    finally:
        stubber.deactivate()
    assert found is not None
    assert found["id"] == "i-match"


def test_find_instance_for_host_returns_none_when_no_tag_match(temp_mngr_ctx: MngrContext) -> None:
    """A host with no matching instance tag resolves to None (after a cache-refresh retry).

    On a cache miss ``_find_instance_for_host`` refreshes the instance list once
    and retries (so a just-created instance absent from a stale cache is still
    found), hence two ``describe_instances`` calls before giving up.
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    no_match = _describe_instances_response(
        [
            {
                "InstanceId": "i-other",
                "State": {"Name": "running"},
                "Tags": [{"Key": "mngr-host-id", "Value": str(HostId.generate())}],
            },
        ]
    )
    stubber.add_response("describe_instances", no_match)
    stubber.add_response("describe_instances", no_match)
    stubber.activate()
    try:
        found = provider._find_instance_for_host(HostId.generate())
    finally:
        stubber.deactivate()
    assert found is None


def test_find_instance_for_host_refuses_duplicate_host_id_tag(temp_mngr_ctx: MngrContext) -> None:
    """Two non-terminated instances sharing a mngr-host-id are refused, not silently disambiguated.

    ``mngr-host-id`` is account-writable, so a duplicate (e.g. an attacker tagging
    their own instance with a victim's host id) could otherwise steer ``mngr start``
    -- and the agent-tag writes keyed off this lookup -- onto the wrong instance.
    The lookup must raise rather than pick the first match.
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-real", "stopped", "", {"mngr-host-id": str(host_id), "mngr-provider": "aws-test"}
                ),
                _instance_with_tags(
                    "i-evil", "running", "", {"mngr-host-id": str(host_id), "mngr-provider": "aws-test"}
                ),
            ]
        ),
    )
    stubber.activate()
    try:
        with pytest.raises(MngrError, match="ambiguous"):
            provider._find_instance_for_host(host_id)
    finally:
        stubber.deactivate()


def test_rebind_known_hosts_pre_connect_uses_local_keypairs(temp_mngr_ctx: MngrContext) -> None:
    """The pre-connect known_hosts rebind pins mngr's own local host keys, not tag data.

    On resume the new IP is added to known_hosts *before* the first SSH. Sourcing
    the host keys from the locally held provider keypairs (injected into the box at
    create) -- rather than from account-writable EC2 tags -- is what prevents an
    attacker who can edit tags from substituting their own host key and MITMing the
    resumed session.
    """
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    new_ip = "203.0.113.50"
    expected_vps_key = provider._get_vps_host_keypair(host_id)[1]
    expected_container_key = provider._get_container_host_keypair(host_id)[1]

    provider._rebind_known_hosts_pre_connect(host_id, new_ip)

    vps_known_hosts = provider._vps_known_hosts_path().read_text()
    container_known_hosts = provider._container_known_hosts_path().read_text()
    assert new_ip in vps_known_hosts and expected_vps_key in vps_known_hosts
    assert new_ip in container_known_hosts and expected_container_key in container_known_hosts


def _instance_with_tags(instance_id: str, state: str, public_ip: str, tags: dict[str, str]) -> dict:
    entry: dict = {"InstanceId": instance_id, "State": {"Name": state}}
    if public_ip:
        entry["PublicIpAddress"] = public_ip
    entry["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]
    return entry


def test_state_store_raises_when_no_bucket(temp_mngr_ctx: MngrContext) -> None:
    """``_state_store`` raises an actionable error when no bucket is resolvable.

    With the EC2 tag mirror removed the S3 state bucket is required infrastructure
    (there is no degraded fallback): when absent, accessing ``_state_store`` raises
    a MngrError pointing at ``mngr aws prepare`` rather than selecting a no-op
    store. (The bucket-present case -- a ``BucketHostStateStore`` -- is covered in
    ``state_bucket_backend_test.py``.)
    """
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    # No bucket name configured and STS unresolvable => _state_bucket is None.
    provider.__dict__["_state_bucket"] = None
    with pytest.raises(MngrError, match="mngr aws prepare"):
        _ = provider._state_store


def test_validate_external_store_ready_raises_when_no_bucket(temp_mngr_ctx: MngrContext) -> None:
    """``create_host``'s pre-launch store check fails fast when the required bucket is absent.

    ``_validate_external_store_ready`` (called by ``create_host`` before any
    instance is launched) touches ``_state_store``, so a missing bucket surfaces
    the actionable ``mngr aws prepare`` error *before* the create path provisions
    anything -- rather than after the instance is already running.
    """
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    provider.__dict__["_state_bucket"] = None
    with pytest.raises(MngrError, match="mngr aws prepare"):
        provider._validate_external_store_ready()


def test_validate_external_store_ready_is_noop_when_store_present(temp_mngr_ctx: MngrContext) -> None:
    """With the state store resolvable, the pre-launch check is a no-op so create proceeds."""
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    # Pre-seed the cached store so the hook finds it without a bucket-existence probe.
    provider.__dict__["_state_store"] = object()
    provider._validate_external_store_ready()


def test_persist_agent_data_no_bucket_raises_for_stopped_host(temp_mngr_ctx: MngrContext) -> None:
    """With no state bucket, mirroring an agent raises (the bucket is required).

    The base on-volume write short-circuits with ``HostNotFoundError`` (seeded
    ``vps_ip=None``), then the offline mirror is attempted. With no bucket the
    state store raises an actionable error pointing at ``mngr aws prepare`` rather
    than silently dropping the record (which would make the stopped host's agents
    vanish). No EC2 tag calls are made (the stubber has no queued responses).
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    provider.__dict__["_state_bucket"] = None
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)
    stubber.activate()
    try:
        with pytest.raises(MngrError, match="mngr aws prepare"):
            provider.persist_agent_data(host_id, {"id": str(agent_id), "name": "a1", "type": "command"})
    finally:
        stubber.deactivate()
    # No EC2 calls were made before the raise.
    stubber.assert_no_pending_responses()


def test_list_persisted_agent_data_no_bucket_raises_actionable_error(temp_mngr_ctx: MngrContext) -> None:
    """An offline agent-record read with no bucket raises MngrError pointing at ``mngr aws prepare``.

    For a stopped host the base SSH/volume read raises ``HostNotFoundError`` and we
    fall back to the offline store; with no bucket that store raises rather than
    silently returning no agents (which would make a stopped host's agents vanish).
    """
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    provider.__dict__["_state_bucket"] = None
    host_id = HostId.generate()
    seed_stopped_host_record(provider, host_id)
    with pytest.raises(MngrError, match="mngr aws prepare"):
        provider.list_persisted_agent_data_for_host(host_id)


def test_to_offline_host_no_bucket_raises_actionable_error(temp_mngr_ctx: MngrContext) -> None:
    """Reconstructing a stopped host's full record with no bucket raises MngrError citing the S3 state bucket."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    provider.__dict__["_state_bucket"] = None
    host_id = HostId.generate()
    # The base path lists instances (none match) => HostNotFoundError, then the
    # override reads the host record from the (missing) store, which raises.
    stubber.add_response("describe_instances", _describe_instances_response([]))
    stubber.add_response("describe_instances", _describe_instances_response([]))
    stubber.activate()
    try:
        with pytest.raises(MngrError, match="S3 state bucket"):
            provider.to_offline_host(host_id)
    finally:
        stubber.deactivate()


def test_discover_surfaces_stopped_instance_but_offline_read_raises_without_bucket(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A stopped instance is identified from identity tags, but reading its agents with no bucket raises.

    Offline discovery still reconstructs a STOPPED host from the cheap identity
    tags, then reads that host's agent records from the state store. With no bucket
    that read raises an actionable MngrError, which propagates so the discovery
    wrapper can attribute the failure to this provider.
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    provider.__dict__["_state_bucket"] = None
    host_id = HostId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-1", "stopped", "", {"mngr-host-id": str(host_id), "mngr-provider": "aws-test", "Name": "mngr-h"}
                )
            ]
        ),
    )
    stubber.activate()
    try:
        with ConcurrencyGroup(name="test") as cg:
            with pytest.raises(MngrError, match="mngr aws prepare"):
                provider.discover_hosts_and_agents(cg)
    finally:
        stubber.deactivate()


def test_offline_discovered_host_from_instance_yields_stopped_host(temp_mngr_ctx: MngrContext) -> None:
    """A stopped instance with ``mngr-host-id`` + ``Name`` tags yields a STOPPED DiscoveredHost with that name.

    Exercises the shared ``OfflineCapableVpsProvider._offline_discovered_host_from_instance``
    default through AWS's ``_host_name_tag_key()`` hook (``Name``).
    """
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    instance = _normalized_instance({"mngr-host-id": str(host_id), "Name": "mngr-myhost"})
    discovered = provider._offline_discovered_host_from_instance(instance)
    assert discovered is not None
    assert discovered.host_id == host_id
    assert str(discovered.host_name) == "myhost"
    assert discovered.host_state == HostState.STOPPED


def test_offline_discovered_host_from_instance_returns_none_without_host_id_tag(temp_mngr_ctx: MngrContext) -> None:
    """An instance lacking the ``mngr-host-id`` identity tag is not a mngr host (returns None)."""
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    assert provider._offline_discovered_host_from_instance(_normalized_instance({"Name": "mngr-other"})) is None


class _RecordingStateBucket:
    """In-memory ``StateBucket`` that records which host's state was deleted.

    Lets the offline-destroy tests assert that ``destroy_host`` removes the
    stopped host's mirrored state (the only externally observable effect of the
    offline teardown besides the EC2 terminate call), without standing up a real
    S3 bucket.
    """

    def __init__(self, *, record_json: str | None = None) -> None:
        self.deleted_host_ids: list[HostId] = []
        self._record_json = record_json

    def write_host_record_json(self, host_id: HostId, record_json: str) -> None: ...

    def read_host_record_json(self, host_id: HostId) -> str | None:
        return self._record_json

    def write_agent_record(self, host_id: HostId, agent_id: str, data: Mapping[str, object]) -> None: ...

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        return []

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None: ...

    def delete_host_state(self, host_id: HostId) -> None:
        self.deleted_host_ids.append(host_id)

    def host_dir_prefix_has_objects(self, host_id: HostId) -> bool:
        return False

    def volume_for_host(self, host_id: HostId) -> Volume:
        raise NotImplementedError


def _seed_state_store_bucket(provider: AwsProvider, *, record_json: str | None = None) -> _RecordingStateBucket:
    """Pre-seed the provider's cached ``_state_store`` with a recording bucket."""
    bucket = _RecordingStateBucket(record_json=record_json)
    provider.__dict__["_state_store"] = BucketHostStateStore(bucket=bucket, bucket_label="test bucket")
    return bucket


def _stopped_host_record_json(host_id: HostId, *, vps_ssh_key_id: str) -> str:
    """A mirrored stopped-host record carrying the per-host SSH key id to clean up on destroy."""
    return VpsHostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="myhost",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            stop_reason=HostState.STOPPED.value,
        ),
        config=VpsHostConfig(
            vps_instance_id=VpsInstanceId("i-stopped"),
            region="us-east-1",
            plan="t3.small",
            container_name="mngr-c",
            volume_name="mngr-vol",
            vps_ssh_key_id=vps_ssh_key_id,
        ),
    ).model_dump_json()


def _stopped_instance_describe_response(instance_id: str, host_id: HostId) -> dict:
    return _describe_instances_response(
        [_instance_with_tags(instance_id, "stopped", "", {"mngr-host-id": str(host_id), "mngr-provider": "aws-test"})]
    )


def test_destroy_host_offline_terminates_instance_and_clears_state(temp_mngr_ctx: MngrContext) -> None:
    """Destroying a STOPPED host terminates its EC2 instance and removes its external state.

    The base SSH/volume teardown raises ``HostNotFoundError`` for a stopped host
    (no reachable vps_ip), so ``OfflineCapableVpsProvider.destroy_host`` must
    fall back to the offline path: resolve the instance by its ``mngr-host-id`` tag,
    terminate it, and delete the mirrored state. This is the fix for the leak where
    ``mngr destroy`` of a stopped host left the instance running.
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    seed_stopped_host_record(provider, host_id)
    bucket = _seed_state_store_bucket(
        provider, record_json=_stopped_host_record_json(host_id, vps_ssh_key_id="mngr-key")
    )
    # The offline path resolves the instance from the tag listing, terminates it,
    # then cleans up the per-host EC2 KeyPair recovered from the mirrored record.
    stubber.add_response("describe_instances", _stopped_instance_describe_response("i-stopped", host_id))
    stubber.add_response("terminate_instances", {})
    stubber.add_response("delete_key_pair", {})
    stubber.activate()
    try:
        provider.destroy_host(host_id)
    finally:
        stubber.deactivate()
    stubber.assert_no_pending_responses()
    assert bucket.deleted_host_ids == [host_id]


def test_destroy_host_offline_fails_loudly_when_terminate_fails(temp_mngr_ctx: MngrContext) -> None:
    """A stopped instance that cannot be terminated raises rather than reporting success.

    A leaked, still-billing instance must never masquerade as a clean destroy: when
    ``terminate_instances`` fails with a non-"already gone" error, the offline
    teardown records a ``HOST_RESOURCE_REMAINS`` failure and raises a
    ``CleanupFailedGroup`` (so the CLI exits non-zero).
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    seed_stopped_host_record(provider, host_id)
    _seed_state_store_bucket(provider)
    stubber.add_response("describe_instances", _stopped_instance_describe_response("i-stuck", host_id))
    stubber.add_client_error("terminate_instances", service_error_code="InternalError", http_status_code=500)
    stubber.activate()
    try:
        with pytest.raises(CleanupFailedGroup) as exc_info:
            provider.destroy_host(host_id)
    finally:
        stubber.deactivate()
    categories = {failure.category for failure in exc_info.value.failures}
    assert CleanupFailureCategory.HOST_RESOURCE_REMAINS in categories


def test_destroy_host_offline_is_idempotent_when_instance_already_gone(temp_mngr_ctx: MngrContext) -> None:
    """Destroying a stopped host whose instance is already terminated still succeeds and clears state.

    A terminated instance is absent from the tag listing, so ``_find_instance_for_host``
    returns None (after its cache-refresh retry => two describe calls). The teardown
    treats that as already-done (no terminate call) and still removes the external
    state, so a re-run of ``destroy`` is idempotent rather than a hard failure.
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    seed_stopped_host_record(provider, host_id)
    bucket = _seed_state_store_bucket(provider)
    stubber.add_response("describe_instances", _describe_instances_response([]))
    stubber.add_response("describe_instances", _describe_instances_response([]))
    stubber.activate()
    try:
        provider.destroy_host(host_id)
    finally:
        stubber.deactivate()
    stubber.assert_no_pending_responses()
    assert bucket.deleted_host_ids == [host_id]


def test_destroy_host_offline_dispatch_ignores_stale_cached_record_with_vps_ip(temp_mngr_ctx: MngrContext) -> None:
    """A stopped host whose cache still holds a vps_ip-set record is destroyed offline, not over SSH.

    Regression guard for the warm-cache case (``mngr stop`` then ``mngr destroy``
    discovery in one process): the cached ``VpsDockerHostRecord`` keeps its
    pre-stop ``vps_ip``, so the base ``destroy_host`` would try a doomed SSH
    teardown against a dead address and leak the still-billing instance. Dispatch
    on the instance's own (stopped) power state instead, so the offline terminate
    runs and no outer SSH connection is ever opened (the sentinel-raising
    ``_make_outer_for_vps_ip`` proves it).
    """
    host_id = HostId.generate()

    class _OuterRaisingAwsProvider(AwsProvider):
        @contextmanager
        def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
            raise _OnVolumeReached("base SSH teardown must not run for a stopped host")
            yield

    base, stubber = _build_stubbed_provider(temp_mngr_ctx)
    provider = _OuterRaisingAwsProvider(
        name=base.name,
        host_dir=base.host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=base.config,
        vps_client=base.aws_client,
        aws_client=base.aws_client,
        aws_config=base.aws_config,
    )
    # Seed the warm cache with a reachable-looking record (vps_ip + config set), as
    # an in-process ``mngr stop`` leaves it; the dispatch must still go offline.
    provider._host_record_cache[host_id] = VpsHostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="myhost",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            stop_reason=HostState.STOPPED.value,
        ),
        vps_ip="1.2.3.4",
        config=VpsHostConfig(
            vps_instance_id=VpsInstanceId("i-stopped"),
            region="us-east-1",
            plan="t3.small",
            container_name="mngr-c",
            volume_name="mngr-vol",
        ),
    )
    bucket = _seed_state_store_bucket(provider)
    stubber.add_response("describe_instances", _stopped_instance_describe_response("i-stopped", host_id))
    stubber.add_response("terminate_instances", {})
    stubber.activate()
    try:
        provider.destroy_host(host_id)
    finally:
        stubber.deactivate()
    stubber.assert_no_pending_responses()
    assert bucket.deleted_host_ids == [host_id]


def _reachable_provider_with_record(temp_mngr_ctx: MngrContext, host_id: HostId) -> AwsProvider:
    """Build a provider with a cached *reachable* (vps_ip set) record for ``host_id``.

    Used by the running-host delegation tests: with a reachable record cached,
    ``super().persist_agent_data`` reaches the on-volume store via
    ``_make_outer_for_vps_ip`` instead of doing discovery / real SSH, so a test
    can confirm the override does NOT bypass the authoritative on-volume path.
    """
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="myhost",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    provider._host_record_cache[host_id] = VpsHostRecord(
        certified_host_data=certified,
        vps_ip="1.2.3.4",
        config=VpsHostConfig(
            vps_instance_id=VpsInstanceId("i-1"),
            region="us-east-1",
            plan="t3.small",
            container_name="mngr-c",
            volume_name="mngr-vol",
        ),
    )
    return provider


class _OnVolumeReached(MngrError):
    """Sentinel raised by the test's outer to prove the on-volume path was entered."""


def test_persist_agent_data_does_not_bypass_on_volume_store_for_running_host(temp_mngr_ctx: MngrContext) -> None:
    """For a running (reachable) host, persist_agent_data delegates to the on-volume base path.

    Regression guard: the override must compose with ``super()`` (the
    authoritative on-volume ``agents/<id>.json`` the SSH-based discovery reads
    for running hosts), not replace it -- otherwise running AWS hosts list with
    no agents. We prove delegation by making the on-volume access raise a unique
    sentinel from ``_make_outer_for_vps_ip``: it surfaces only if ``super()`` was
    actually invoked, and -- unlike ``HostNotFoundError`` -- is *not* swallowed,
    so a genuine running-host write failure is never silently hidden.
    """
    host_id = HostId.generate()

    class _OuterRaisingProvider(AwsProvider):
        @contextmanager
        def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
            # The unreachable yield below keeps this a generator function (which
            # @contextmanager requires); the raise fires before it is ever reached.
            raise _OnVolumeReached("on-volume persist path entered")
            yield

    base = _reachable_provider_with_record(temp_mngr_ctx, host_id)
    provider = _OuterRaisingProvider(
        name=base.name,
        host_dir=base.host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=base.config,
        vps_client=base.aws_client,
        aws_client=base.aws_client,
        aws_config=base.aws_config,
    )
    provider._host_record_cache[host_id] = base._host_record_cache[host_id]

    with pytest.raises(_OnVolumeReached):
        provider.persist_agent_data(host_id, {"id": "agent-1", "name": "a1", "type": "command"})


def _normalized_instance(tag_pairs: dict[str, str]) -> dict:
    """A normalized instance dict (``{"id", "tags": ["k=v", ...]}``) for offline-discovery helper tests."""
    return {"id": "i-1", "tags": [f"{k}={v}" for k, v in tag_pairs.items()]}


def test_realizer_for_instance_reads_bare_marker_from_ec2_tags(temp_mngr_ctx: MngrContext) -> None:
    """An EC2 bare host (tag ``mngr-isolation=none``) resolves to the BARE realizer.

    AWS stamps the placement marker as a plain EC2 tag, read back in the normalized
    ``key=value`` list. The provider config defaults to CONTAINER, but the host's own
    placement marker selects the bare realizer -- the fix that makes a bare host
    discoverable/reachable without re-specifying isolation at connect time.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    bare = _normalized_instance({"mngr-host-id": "h-1", ISOLATION_TAG_KEY: "none"})
    assert isinstance(provider._realizer_for_instance(bare), BareRealizer)
    # An untagged (pre-marker) instance defaults to the container realizer.
    legacy = _normalized_instance({"mngr-host-id": "h-2"})
    assert isinstance(provider._realizer_for_instance(legacy), DockerRealizer)


def test_validate_provider_args_under_pytest_raises_when_unset(
    temp_mngr_ctx: MngrContext,
) -> None:
    """The pre-create hook fires when auto_shutdown_seconds is None (the config default).

    Regression: a release test that forgets to set auto_shutdown_seconds on
    the AWS provider config would silently launch instances with no self-
    termination safety net. The hook must abort the launch before any
    EC2 API call so the leak window is zero.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=None)
    with pytest.raises(MngrError, match="auto_shutdown_seconds"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_accepts_positive(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Properly configured tests pass the hook and proceed to instance creation."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    # No exception raised.
    provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_raises_when_zero(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Zero (and negatives) are explicitly rejected, not silently treated as unset."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=0)
    with pytest.raises(MngrError, match="auto_shutdown_seconds"):
        provider._validate_provider_args_for_create()


# =============================================================================
# AWS build-args parser (--aws-region, --aws-instance-type, --aws-ami, --git-depth)
# =============================================================================


def test_parse_build_args_uses_defaults_when_none(temp_mngr_ctx: MngrContext) -> None:
    """No build args -> region / instance-type come from the provider config; ami override stays None."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(None)
    assert parsed.region == provider.aws_config.default_region
    assert parsed.plan == provider.aws_config.default_instance_type
    assert parsed.ami_id_override is None
    assert parsed.git_depth is None
    assert parsed.docker_build_args == ()


def test_parse_build_args_accepts_aws_ami_override(temp_mngr_ctx: MngrContext) -> None:
    """`--aws-ami=ami-XYZ` lands on ami_id_override; other fields keep their defaults."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(["--aws-ami=ami-0123abcd"])
    assert parsed.ami_id_override == "ami-0123abcd"
    assert parsed.region == provider.aws_config.default_region
    assert parsed.plan == provider.aws_config.default_instance_type


def test_parse_build_args_extracts_all_aws_knobs_plus_docker_passthrough(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Each AWS-prefixed knob is peeled off; the remainder forwards to docker verbatim."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(
        [
            "--aws-region=us-west-2",
            "--aws-instance-type=t3.medium",
            "--aws-ami=ami-deadbeef",
            "--aws-spot",
            "--git-depth=1",
            "--file=Dockerfile",
            ".",
        ]
    )
    assert parsed.region == "us-west-2"
    assert parsed.plan == "t3.medium"
    assert parsed.ami_id_override == "ami-deadbeef"
    assert parsed.spot is True
    assert parsed.git_depth == 1
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_spot_defaults_false(temp_mngr_ctx: MngrContext) -> None:
    """Without --aws-spot, the parsed object reports spot=False (default on-demand)."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    parsed = provider._parse_build_args(None)
    assert parsed.spot is False


def test_parse_build_args_rejects_aws_spot_with_value(temp_mngr_ctx: MngrContext) -> None:
    """``--aws-spot`` is presence-only; passing a value (e.g. ``--aws-spot=true``) raises."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    with pytest.raises(MngrError, match="presence-only flag"):
        provider._parse_build_args(["--aws-spot=true"])


def test_parse_build_args_rejects_unknown_aws_flag(temp_mngr_ctx: MngrContext) -> None:
    """A typo / unknown --aws-* flag raises with the valid-args list, not silently forwarded."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    with pytest.raises(MngrError, match="Unknown aws build arg.*--aws-bogus"):
        provider._parse_build_args(["--aws-bogus=foo"])


def test_parse_build_args_rejects_dropped_vps_prefix(temp_mngr_ctx: MngrContext) -> None:
    """A caller still using --vps-region= gets the migration error pointing at the new name."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=60)
    with pytest.raises(MngrError, match="no longer supported"):
        provider._parse_build_args(["--vps-region=us-east-1"])


# =============================================================================
# Read paths surface auth failures as ProviderUnavailableError (not ...Empty)
# =============================================================================
#
# Missing credentials means the backend's state is *unknown* -- we couldn't
# authenticate, so any running instances are hidden from us. Per the
# ``ProviderEmptyError`` vs ``ProviderUnavailableError`` contract in
# ``mngr.errors``, that's the ``Unavailable`` shape: "could not be reached",
# agents may still exist. The shared discovery loop in
# ``mngr.api.list._construct_and_discover_for_provider`` catches
# ``ProviderUnavailableError`` via its generic catch-all and logs it at error
# level, so the misconfiguration is visible without the backend needing its
# own warning.
#
# AMI selection is a create-only concern (read paths do not need it to
# enumerate or reach existing instances). ``build_provider_instance`` never
# touches AMI resolution; that lives in ``AwsProvider._create_vps_instance``,
# the only call site, and a missing-AMI failure there raises ``MngrError``
# (a config error to be fixed). Create-path missing-creds is surfaced
# identically to read paths because the create flow calls
# ``build_provider_instance`` first -- no ``bootstrap_for_host_creation``
# override is needed, matching the Azure pattern.


def test_build_provider_instance_raises_unavailable_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
) -> None:
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_ami_id="ami-deadbeef")
    name = ProviderInstanceName("aws-test")

    with pytest.raises(ProviderUnavailableError):
        AwsProviderBackend.build_provider_instance(name=name, config=config, mngr_ctx=temp_mngr_ctx)


def test_build_provider_instance_does_not_touch_ami_resolution(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A provider with valid creds but no AMI configured must still list/discover.

    AMI is a create-only concern; resolving it during ``build_provider_instance``
    would misclassify a misconfigured-AMI provider as unreachable and hide its
    already-running instances from ``mngr list`` / ``connect`` / ``gc``. This
    test pins the contract: build must succeed when only credentials resolve.
    """
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_region="ap-south-1",
        default_ami_id=None,
    )
    name = ProviderInstanceName("aws-test")

    provider = AwsProviderBackend.build_provider_instance(name=name, config=config, mngr_ctx=temp_mngr_ctx)

    assert isinstance(provider, AwsProvider)


def test_create_vps_instance_raises_mngr_error_when_no_ami_configured(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Missing AMI is a create-time config error (MngrError), not a state signal.

    Distinct from the missing-creds case: ``ProviderUnavailableError`` would
    misclassify "I have valid creds but the operator forgot to pin a
    ``default_ami_id``" as an unreachable backend. The right shape at the
    create path is a plain ``MngrError`` carrying the actionable how-to-fix
    from ``AwsProviderConfig.get_ami_id_for_region``. The create flow's
    ``create_host`` except handler cleans up any SSH key uploaded before this
    raise, so no leak.
    """
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_region="ap-south-1",
        default_ami_id=None,
    )
    name = ProviderInstanceName("aws-test")
    provider = AwsProviderBackend.build_provider_instance(name=name, config=config, mngr_ctx=temp_mngr_ctx)
    assert isinstance(provider, AwsProvider)
    parsed = ParsedAwsBuildOptions(
        region=config.default_region, plan=config.default_instance_type, docker_build_args=()
    )

    with pytest.raises(MngrError, match="No AMI configured"):
        provider._create_vps_instance(parsed=parsed, label="test", user_data="", ssh_key_ids=(), tags={})
