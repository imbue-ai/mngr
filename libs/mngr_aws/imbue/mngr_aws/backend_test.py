"""Tests for AWS provider backend registration."""

from collections.abc import Iterator
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
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws.backend import AWS_BACKEND_NAME
from imbue.mngr_aws.backend import AwsProvider
from imbue.mngr_aws.backend import AwsProviderBackend
from imbue.mngr_aws.backend import IDLE_SENTINEL_FILENAME
from imbue.mngr_aws.backend import IDLE_WATCHER_UNIT_NAME
from imbue.mngr_aws.backend import ParsedAwsBuildOptions
from imbue.mngr_aws.backend import _build_idle_watcher_path_unit
from imbue.mngr_aws.backend import _build_idle_watcher_service_unit
from imbue.mngr_aws.backend import _build_sentinel_shutdown_script
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.testing import _StubbedAwsVpsClient
from imbue.mngr_aws.testing import clear_aws_env
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import VpsHostConfig
from imbue.mngr_vps_docker.primitives import VpsInstanceId


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


def _seed_stopped_host_record(provider: AwsProvider, host_id: HostId) -> None:
    """Cache a record with ``vps_ip=None`` so the base on-volume path short-circuits.

    The agent-data hooks call ``super()`` first (the authoritative on-volume
    store) and only fall back to / additionally write EC2 tags. For a *stopped*
    host the base raises ``HostNotFoundError`` (no reachable ``vps_ip``); seeding
    such a record makes the base short-circuit immediately without any SSH or
    discovery sweep, so these tag-path tests exercise the stopped-host fallback
    without standing up a fake VPS.
    """
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="myhost",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        stop_reason=HostState.STOPPED.value,
    )
    provider._host_record_cache[host_id] = VpsDockerHostRecord(certified_host_data=certified)


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
    new_ip = "203.0.113.50"
    expected_vps_key = provider._get_vps_host_keypair()[1]
    expected_container_key = provider._get_container_host_keypair()[1]

    provider._rebind_known_hosts_pre_connect(new_ip)

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


def test_persist_agent_data_writes_per_field_agent_tags(temp_mngr_ctx: MngrContext) -> None:
    """persist_agent_data finds the instance by host tag and upserts per-field mngr-agent-<id>-* tags.

    Exercises the stopped-host path (the on-volume base write is unavailable, so
    only the EC2 tags are written); the seeded ``vps_ip=None`` record makes
    ``super().persist_agent_data`` short-circuit with ``HostNotFoundError``. The
    agent id is carried in the tag key, and name/type each get their own tag; with
    no labels here, no ``-labels`` tag is written and (no stale keys exist) nothing
    is deleted.
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    _seed_stopped_host_record(provider, host_id)
    stubber.add_response(
        "describe_instances",
        _describe_instances_response([_instance_with_tags("i-1", "stopped", "", {"mngr-host-id": str(host_id)})]),
    )
    stubber.add_response(
        "create_tags",
        {},
        expected_params={
            "Resources": ["i-1"],
            "Tags": [
                {"Key": f"mngr-agent-{agent_id}-name", "Value": "a1"},
                {"Key": f"mngr-agent-{agent_id}-type", "Value": "command"},
            ],
        },
    )
    stubber.activate()
    try:
        provider.persist_agent_data(
            host_id,
            {"id": str(agent_id), "name": "a1", "type": "command", "command": "sleep 1", "work_dir": "/w"},
        )
    finally:
        stubber.deactivate()


def test_persist_agent_data_writes_labels_in_their_own_tag(temp_mngr_ctx: MngrContext) -> None:
    """An agent with labels gets a dedicated ``mngr-agent-<id>-labels`` tag (compact JSON)."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    _seed_stopped_host_record(provider, host_id)
    stubber.add_response(
        "describe_instances",
        _describe_instances_response([_instance_with_tags("i-1", "stopped", "", {"mngr-host-id": str(host_id)})]),
    )
    stubber.add_response(
        "create_tags",
        {},
        expected_params={
            "Resources": ["i-1"],
            "Tags": [
                {"Key": f"mngr-agent-{agent_id}-name", "Value": "a1"},
                {"Key": f"mngr-agent-{agent_id}-type", "Value": "command"},
                {"Key": f"mngr-agent-{agent_id}-labels", "Value": '{"env":"prod"}'},
            ],
        },
    )
    stubber.activate()
    try:
        provider.persist_agent_data(
            host_id,
            {"id": str(agent_id), "name": "a1", "type": "command", "labels": {"env": "prod"}},
        )
    finally:
        stubber.deactivate()


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
    provider._host_record_cache[host_id] = VpsDockerHostRecord(
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


def test_list_persisted_agent_data_for_host_reads_tags(temp_mngr_ctx: MngrContext) -> None:
    """list_persisted_agent_data_for_host reassembles an agent from its per-field tags (stopped host)."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-1",
                    "stopped",
                    "",
                    {
                        "mngr-host-id": str(host_id),
                        f"mngr-agent-{agent_id}-name": "a1",
                        f"mngr-agent-{agent_id}-type": "command",
                        f"mngr-agent-{agent_id}-labels": '{"env":"prod"}',
                    },
                )
            ]
        ),
    )
    stubber.activate()
    try:
        agents = provider.list_persisted_agent_data_for_host(host_id)
    finally:
        stubber.deactivate()
    assert len(agents) == 1
    assert agents[0]["id"] == str(agent_id)
    assert agents[0]["name"] == "a1"
    assert agents[0]["type"] == "command"
    assert agents[0]["labels"] == {"env": "prod"}


def test_list_persisted_agent_data_skips_malformed_labels_tag(
    temp_mngr_ctx: MngrContext, log_warnings: list[str]
) -> None:
    """A ``-labels`` tag that is valid JSON but not an object is dropped (warn), not crashed on.

    mngr only ever writes object-shaped labels, so a scalar/array value means an
    externally edited/corrupted tag. Reassembly must degrade gracefully: skip just
    the labels for that agent (the agent still surfaces via its name/type tags) and
    log a warning, rather than letting a malformed value crash the whole discovery
    sweep for every host.
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-1",
                    "stopped",
                    "",
                    {
                        "mngr-host-id": str(host_id),
                        f"mngr-agent-{agent_id}-name": "a1",
                        # Valid JSON, but a bare integer rather than an object.
                        f"mngr-agent-{agent_id}-labels": "5",
                    },
                )
            ]
        ),
    )
    stubber.activate()
    try:
        agents = provider.list_persisted_agent_data_for_host(host_id)
    finally:
        stubber.deactivate()
    assert len(agents) == 1
    assert agents[0]["id"] == str(agent_id)
    assert agents[0]["name"] == "a1"
    assert "labels" not in agents[0]
    assert any("not a JSON object" in w for w in log_warnings), log_warnings


def test_discover_hosts_and_agents_surfaces_stopped_host_from_tags(temp_mngr_ctx: MngrContext) -> None:
    """A stopped instance (no public IP) is reconstructed from tags as a STOPPED host with its agents."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-1",
                    "stopped",
                    "",
                    {
                        "mngr-host-id": str(host_id),
                        "mngr-provider": "aws-test",
                        "Name": "mngr-myhost",
                        f"mngr-agent-{agent_id}-name": "a1",
                        f"mngr-agent-{agent_id}-type": "command",
                    },
                )
            ]
        ),
    )
    stubber.activate()
    try:
        with ConcurrencyGroup(name="test") as cg:
            result = provider.discover_hosts_and_agents(cg)
    finally:
        stubber.deactivate()
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
    """A host whose instance is still ``stopping`` (OS already down) is reconstructed from tags.

    Regression: the offline reconstruction used to require raw state ``stopped``, so a
    host vanished from discovery for the seconds-long stop transition -- making
    `mngr start <agent>` race a "not found" if it landed mid-stop. A stopping instance
    must surface (as STOPPED) so resolve-by-name is stable across the transition.
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-1",
                    "stopping",
                    "",
                    {
                        "mngr-host-id": str(host_id),
                        "mngr-provider": "aws-test",
                        "Name": "mngr-myhost",
                        f"mngr-agent-{agent_id}-name": "a1",
                    },
                )
            ]
        ),
    )
    stubber.activate()
    try:
        with ConcurrencyGroup(name="test") as cg:
            result = provider.discover_hosts_and_agents(cg)
    finally:
        stubber.deactivate()
    hosts = {host.host_id: host for host in result}
    assert host_id in hosts
    assert hosts[host_id].host_state == HostState.STOPPED
    assert [a.agent_id for a in result[hosts[host_id]]] == [agent_id]


def test_discover_hosts_and_agents_skips_instance_with_malformed_host_id_tag(
    temp_mngr_ctx: MngrContext, log_warnings: list[str]
) -> None:
    """One instance with a corrupt mngr-host-id tag must not abort discovery for the others.

    A malformed mngr-host-id yields an invalid ``HostId`` (a ``ValueError``); the
    offline-discovery loop must skip just that instance (with a warning) and still
    surface the well-formed stopped host, rather than letting one bad tag take down
    the whole sweep (which would break ``mngr list`` / ``mngr start`` account-wide).
    """
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    good_host_id = HostId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-bad", "stopped", "", {"mngr-host-id": "not-a-valid-host-id", "mngr-provider": "aws-test"}
                ),
                _instance_with_tags(
                    "i-good",
                    "stopped",
                    "",
                    {"mngr-host-id": str(good_host_id), "mngr-provider": "aws-test", "Name": "mngr-goodhost"},
                ),
            ]
        ),
    )
    stubber.activate()
    try:
        with ConcurrencyGroup(name="test") as cg:
            result = provider.discover_hosts_and_agents(cg)
    finally:
        stubber.deactivate()
    host_ids = {host.host_id for host in result}
    assert good_host_id in host_ids, "well-formed stopped host should still surface"
    assert any("malformed mngr host identity" in w.lower() for w in log_warnings), log_warnings


def test_to_offline_host_reconstructs_stopped_host_from_tags(temp_mngr_ctx: MngrContext) -> None:
    """to_offline_host rebuilds a STOPPED offline host from tags when the base SSH path can't reach it."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-1",
                    "stopped",
                    "",
                    {
                        "mngr-host-id": str(host_id),
                        "Name": "mngr-myhost",
                        "mngr-created-at": "2026-01-01T00:00:00+00:00",
                    },
                )
            ]
        ),
    )
    stubber.activate()
    try:
        offline = provider.to_offline_host(host_id)
    finally:
        stubber.deactivate()
    assert offline.id == host_id
    assert str(offline.get_certified_data().host_name) == "myhost"
    assert offline.get_state() == HostState.STOPPED


def test_to_offline_host_warns_on_malformed_created_at_tag(
    temp_mngr_ctx: MngrContext, log_warnings: list[str]
) -> None:
    """A malformed mngr-created-at tag is surfaced (warning) and falls back to now(), not silently swallowed."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-1",
                    "stopped",
                    "",
                    {"mngr-host-id": str(host_id), "Name": "mngr-myhost", "mngr-created-at": "not-a-timestamp"},
                )
            ]
        ),
    )
    stubber.activate()
    try:
        offline = provider.to_offline_host(host_id)
    finally:
        stubber.deactivate()
    assert offline.id == host_id
    assert offline.get_state() == HostState.STOPPED
    assert any("Malformed mngr-created-at" in w for w in log_warnings), log_warnings


def _normalized_instance(tag_pairs: dict[str, str]) -> dict:
    """A normalized instance dict (``{"id", "tags": ["k=v", ...]}``) for tag-helper unit tests."""
    return {"id": "i-1", "tags": [f"{k}={v}" for k, v in tag_pairs.items()]}


def test_agent_field_tags_builds_one_tag_per_field(temp_mngr_ctx: MngrContext) -> None:
    """name/type/labels each map to their own mngr-agent-<id>-<field> tag; the id is in the key."""
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    set_tags, delete_keys = provider._agent_field_tags(
        "agent-1",
        {"id": "agent-1", "name": "a1", "type": "command", "labels": {"env": "prod"}},
        _normalized_instance({"mngr-host-id": "h"}),
    )
    assert set_tags == {
        "mngr-agent-agent-1-name": "a1",
        "mngr-agent-agent-1-type": "command",
        "mngr-agent-agent-1-labels": '{"env":"prod"}',
    }
    assert delete_keys == []


def test_agent_field_tags_omits_empty_labels(temp_mngr_ctx: MngrContext) -> None:
    """An agent with absent or empty labels gets no -labels tag."""
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    instance = _normalized_instance({})
    for agent_data in (
        {"id": "agent-1", "name": "a1", "type": "command"},
        {"id": "agent-1", "name": "a1", "type": "command", "labels": {}},
    ):
        set_tags, _ = provider._agent_field_tags("agent-1", agent_data, instance)
        assert "mngr-agent-agent-1-labels" not in set_tags


def test_agent_field_tags_drops_oversized_labels_with_warning(
    temp_mngr_ctx: MngrContext, log_warnings: list[str]
) -> None:
    """Labels too large for a 256-char tag are dropped (name/type kept) with a warning, not a failure."""
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    set_tags, _ = provider._agent_field_tags(
        "agent-1",
        {"id": "agent-1", "name": "a1", "type": "command", "labels": {"k": "x" * 300}},
        _normalized_instance({}),
    )
    assert set_tags == {"mngr-agent-agent-1-name": "a1", "mngr-agent-agent-1-type": "command"}
    assert any("exceeds the" in w and "labels" in w for w in log_warnings), log_warnings


def test_agent_field_tags_deletes_stale_labels_on_explicit_removal(temp_mngr_ctx: MngrContext) -> None:
    """When an update carries empty labels (an explicit removal), the stale -labels tag is deleted."""
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    instance = _normalized_instance(
        {
            "mngr-agent-agent-1-name": "a1",
            "mngr-agent-agent-1-type": "command",
            "mngr-agent-agent-1-labels": '{"env":"prod"}',
        }
    )
    set_tags, delete_keys = provider._agent_field_tags(
        "agent-1", {"id": "agent-1", "name": "a1", "type": "command", "labels": {}}, instance
    )
    assert "mngr-agent-agent-1-labels" not in set_tags
    assert delete_keys == ["mngr-agent-agent-1-labels"]


def test_agent_field_tags_preserves_absent_fields_on_partial_update(temp_mngr_ctx: MngrContext) -> None:
    """A partial persist (e.g. only id+type) must NOT delete the agent's existing name/labels tags.

    Regression: persist_agent_data is an upsert sometimes called with a partial
    record. Treating an absent field as a removal would clobber the name tag that
    offline resolve-by-name (`mngr start <agent>` on a stopped host) depends on, so
    a stopped agent would become unresolvable after any partial update.
    """
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    instance = _normalized_instance(
        {
            "mngr-agent-agent-1-name": "a1",
            "mngr-agent-agent-1-type": "command",
            "mngr-agent-agent-1-labels": '{"env":"prod"}',
        }
    )
    set_tags, delete_keys = provider._agent_field_tags("agent-1", {"id": "agent-1", "type": "claude"}, instance)
    assert set_tags == {"mngr-agent-agent-1-type": "claude"}
    # name and labels are absent from this update, so their tags are left untouched.
    assert delete_keys == []


def test_persisted_agent_dicts_reassembles_id_with_dashes(temp_mngr_ctx: MngrContext) -> None:
    """An agent id containing dashes still reassembles: the field is split off the *final* dash."""
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    agents = provider._persisted_agent_dicts_from_instance(
        _normalized_instance({"mngr-agent-ab-cd-ef-name": "a1", "mngr-agent-ab-cd-ef-type": "command"})
    )
    assert agents == [{"id": "ab-cd-ef", "name": "a1", "type": "command"}]


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
        default_ami_id="",
        default_ami_by_region={},
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
        default_ami_id="",
        default_ami_by_region={},
    )
    name = ProviderInstanceName("aws-test")
    provider = AwsProviderBackend.build_provider_instance(name=name, config=config, mngr_ctx=temp_mngr_ctx)
    assert isinstance(provider, AwsProvider)
    parsed = ParsedAwsBuildOptions(
        region=config.default_region, plan=config.default_instance_type, docker_build_args=()
    )

    with pytest.raises(MngrError, match="No AMI configured"):
        provider._create_vps_instance(parsed=parsed, label="test", user_data="", ssh_key_ids=(), tags={})


def test_build_sentinel_shutdown_script_touches_the_sentinel_path() -> None:
    """The in-container shutdown script signals idle by touching the given sentinel file.

    The sentinel path is the only contract that ties the in-container write to
    the host-side path unit's ``PathExists``, so it must appear verbatim (quoted
    against spaces) and the script must ``touch`` it rather than kill pid 1.
    """
    sentinel = f"/mngr-vol/host_dir/commands/{IDLE_SENTINEL_FILENAME}"
    script = _build_sentinel_shutdown_script(sentinel)
    assert script.startswith("#!/bin/bash\n")
    assert f'touch "{sentinel}"' in script
    assert "kill -TERM 1" not in script, "AWS shutdown must NOT kill the container; it signals via a sentinel"


def test_build_idle_watcher_path_unit_watches_sentinel_and_targets_service() -> None:
    """The systemd .path unit fires the watcher service when the sentinel appears.

    ``PathExists`` must point at the outer-filesystem sentinel location and
    ``Unit`` must name the paired ``.service`` so the trigger is wired correctly.
    """
    sentinel = f"/mngr-btrfs/abc123/host_dir/commands/{IDLE_SENTINEL_FILENAME}"
    unit = _build_idle_watcher_path_unit(sentinel)
    assert f"PathExists={sentinel}" in unit
    assert f"Unit={IDLE_WATCHER_UNIT_NAME}.service" in unit
    assert "WantedBy=multi-user.target" in unit


def test_build_idle_watcher_service_unit_removes_sentinel_then_powers_off() -> None:
    """The oneshot .service removes the sentinel, then powers the host off via ``shutdown -P now``.

    Powering off (rather than calling the EC2 API) means no IAM role or awscli is
    needed: EC2's ``InstanceInitiatedShutdownBehavior`` decides stop-vs-terminate.
    The sentinel ``rm -f`` must run BEFORE the power-off so a later resume isn't
    immediately re-stopped by the path unit.
    """
    sentinel = "/mngr-btrfs/deadbeef/host_dir/commands/stop-instance-requested"
    unit = _build_idle_watcher_service_unit(sentinel)
    assert "Type=oneshot" in unit
    assert "shutdown -P now" in unit
    assert f"rm -f {sentinel}" in unit
    # rm must precede the power-off so resume gets a clean slate.
    assert unit.index("rm -f") < unit.index("shutdown -P now")
    # No IAM/awscli path: the watcher powers the host off, it does not call the EC2 API.
    assert "stop-instances" not in unit
    assert "--instance-ids" not in unit
    assert "--region" not in unit
