"""Tests for AWS provider backend registration."""

import json

import boto3
import pytest
from botocore.stub import Stubber

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_aws.backend import AWS_BACKEND_NAME
from imbue.mngr_aws.backend import AwsProvider
from imbue.mngr_aws.backend import AwsProviderBackend
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.conftest import clear_aws_env
from imbue.mngr_aws.testing import _StubbedAwsVpsClient


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


def _build_provider(mngr_ctx: MngrContext, *, auto_shutdown_minutes: int | None) -> AwsProvider:
    """Construct an AwsProvider with the given auto-shutdown setting.

    Uses a plain boto3 Session and a placeholder AMI: this helper is only
    used by tests that exercise the pytest-detection guard, which fires
    before any EC2 API call, so the session/AMI are never touched.
    """
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_ami_id="ami-placeholder",
        auto_shutdown_minutes=auto_shutdown_minutes,
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
    config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_ami_id="ami-x", auto_shutdown_minutes=60)
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
    """A host with no matching instance tag (e.g. terminated and gone) resolves to None."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                {
                    "InstanceId": "i-other",
                    "State": {"Name": "running"},
                    "Tags": [{"Key": "mngr-host-id", "Value": str(HostId.generate())}],
                },
            ]
        ),
    )
    stubber.activate()
    try:
        found = provider._find_instance_for_host(HostId.generate())
    finally:
        stubber.deactivate()
    assert found is None


def _instance_with_tags(instance_id: str, state: str, public_ip: str, tags: dict[str, str]) -> dict:
    entry: dict = {"InstanceId": instance_id, "State": {"Name": state}}
    if public_ip:
        entry["PublicIpAddress"] = public_ip
    entry["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]
    return entry


def test_persist_agent_data_writes_compact_agent_tag(temp_mngr_ctx: MngrContext) -> None:
    """persist_agent_data finds the instance by host tag and upserts a compact mngr-agent-<id> tag."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [_instance_with_tags("i-1", "running", "1.2.3.4", {"mngr-host-id": str(host_id)})]
        ),
    )
    expected_value = json.dumps({"id": str(agent_id), "name": "a1", "type": "command"}, separators=(",", ":"))
    stubber.add_response(
        "create_tags",
        {},
        expected_params={"Resources": ["i-1"], "Tags": [{"Key": f"mngr-agent-{agent_id}", "Value": expected_value}]},
    )
    stubber.activate()
    try:
        provider.persist_agent_data(
            host_id,
            {"id": str(agent_id), "name": "a1", "type": "command", "command": "sleep 1", "work_dir": "/w"},
        )
    finally:
        stubber.deactivate()


def test_list_persisted_agent_data_for_host_reads_tags(temp_mngr_ctx: MngrContext) -> None:
    """list_persisted_agent_data_for_host parses mngr-agent-* tags off a (stopped) instance."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    agent_json = json.dumps({"id": str(agent_id), "name": "a1", "type": "command"}, separators=(",", ":"))
    stubber.add_response(
        "describe_instances",
        _describe_instances_response(
            [
                _instance_with_tags(
                    "i-1", "stopped", "", {"mngr-host-id": str(host_id), f"mngr-agent-{agent_id}": agent_json}
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


def test_discover_hosts_and_agents_surfaces_stopped_host_from_tags(temp_mngr_ctx: MngrContext) -> None:
    """A stopped instance (no public IP) is reconstructed from tags as a STOPPED host with its agents."""
    provider, stubber = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    agent_json = json.dumps({"id": str(agent_id), "name": "a1", "type": "command"}, separators=(",", ":"))
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
                        f"mngr-agent-{agent_id}": agent_json,
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


def test_compact_agent_tag_value_falls_back_to_minimal_when_too_long(temp_mngr_ctx: MngrContext) -> None:
    """When id+name+type would exceed the 256-char tag limit, type is dropped (id+name still fit)."""
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    value = provider._compact_agent_tag_value({"id": "agent-1", "name": "a1", "type": "x" * 300})
    assert value is not None
    assert len(value) <= 256
    assert json.loads(value) == {"id": "agent-1", "name": "a1"}


def test_compact_agent_tag_value_none_without_id_or_name(temp_mngr_ctx: MngrContext) -> None:
    """No id or no name -> None (nothing resolvable to persist)."""
    provider, _stubber = _build_stubbed_provider(temp_mngr_ctx)
    assert provider._compact_agent_tag_value({"name": "a1"}) is None
    assert provider._compact_agent_tag_value({"id": "agent-1"}) is None


def test_validate_provider_args_under_pytest_raises_when_unset(
    temp_mngr_ctx: MngrContext,
) -> None:
    """The pre-create hook fires when auto_shutdown_minutes is None (the config default).

    Regression: a release test that forgets to set auto_shutdown_minutes on
    the AWS provider config would silently launch instances with no self-
    termination safety net. The hook must abort the launch before any
    EC2 API call so the leak window is zero.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=None)
    with pytest.raises(MngrError, match="auto_shutdown_minutes"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_accepts_positive(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Properly configured tests pass the hook and proceed to instance creation."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    # No exception raised.
    provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_raises_when_zero(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Zero (and negatives) are explicitly rejected, not silently treated as unset."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=0)
    with pytest.raises(MngrError, match="auto_shutdown_minutes"):
        provider._validate_provider_args_for_create()


# =============================================================================
# AWS build-args parser (--aws-region, --aws-instance-type, --aws-ami, --git-depth)
# =============================================================================


def test_parse_build_args_uses_defaults_when_none(temp_mngr_ctx: MngrContext) -> None:
    """No build args -> region / instance-type come from the provider config; ami override stays None."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    parsed = provider._parse_build_args(None)
    assert parsed.region == provider.aws_config.default_region
    assert parsed.plan == provider.aws_config.default_instance_type
    assert parsed.ami_id_override is None
    assert parsed.git_depth is None
    assert parsed.docker_build_args == ()


def test_parse_build_args_accepts_aws_ami_override(temp_mngr_ctx: MngrContext) -> None:
    """`--aws-ami=ami-XYZ` lands on ami_id_override; other fields keep their defaults."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    parsed = provider._parse_build_args(["--aws-ami=ami-0123abcd"])
    assert parsed.ami_id_override == "ami-0123abcd"
    assert parsed.region == provider.aws_config.default_region
    assert parsed.plan == provider.aws_config.default_instance_type


def test_parse_build_args_extracts_all_aws_knobs_plus_docker_passthrough(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Each AWS-prefixed knob is peeled off; the remainder forwards to docker verbatim."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
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
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    parsed = provider._parse_build_args(None)
    assert parsed.spot is False


def test_parse_build_args_rejects_aws_spot_with_value(temp_mngr_ctx: MngrContext) -> None:
    """``--aws-spot`` is presence-only; passing a value (e.g. ``--aws-spot=true``) raises."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    with pytest.raises(MngrError, match="presence-only flag"):
        provider._parse_build_args(["--aws-spot=true"])


def test_parse_build_args_rejects_unknown_aws_flag(temp_mngr_ctx: MngrContext) -> None:
    """A typo / unknown --aws-* flag raises with the valid-args list, not silently forwarded."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    with pytest.raises(MngrError, match="Unknown aws build arg.*--aws-bogus"):
        provider._parse_build_args(["--aws-bogus=foo"])


def test_parse_build_args_rejects_dropped_vps_prefix(temp_mngr_ctx: MngrContext) -> None:
    """A caller still using --vps-region= gets the migration error pointing at the new name."""
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_minutes=60)
    with pytest.raises(MngrError, match="no longer supported"):
        provider._parse_build_args(["--vps-region=us-east-1"])


# =============================================================================
# Read-path discovery skip is user-visible, but the create path stays quiet
# =============================================================================
#
# ``build_provider_instance`` raises ``ProviderEmptyError`` when AWS credentials
# or AMIs cannot be resolved. The shared discovery code in
# ``mngr.api.list._construct_and_discover_for_provider`` swallows that
# exception at ``logger.debug``, so ``build_provider_instance`` emits a
# ``logger.warning`` for misconfigured providers to remain visible in
# ``mngr list`` / ``mngr connect`` / ``mngr gc``.
#
# The create path must NOT emit that warning: ``mngr create`` resolves the same
# credentials + AMI first via ``bootstrap_for_host_creation``, which surfaces
# the error directly so build's warning is never reached. These tests lock in
# both halves: read paths warn exactly once; the create path raises the same
# error with no misleading "skipping discovery" line, and succeeds quietly
# when credentials + AMI resolve.


def test_build_provider_instance_warns_and_raises_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_ami_id="ami-deadbeef")
    name = ProviderInstanceName("aws-test")

    with pytest.raises(ProviderEmptyError):
        AwsProviderBackend.build_provider_instance(name=name, config=config, mngr_ctx=temp_mngr_ctx)

    assert len(log_warnings) == 1, f"expected exactly one warning, got {log_warnings!r}"
    assert "aws-test" in log_warnings[0]
    assert "skipping discovery" in log_warnings[0]


def test_build_provider_instance_warns_and_raises_when_no_ami_configured(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """Credentials resolve but no AMI does -- the second failure mode still warns."""
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_ami_id="",
        default_ami_by_region={},
    )
    name = ProviderInstanceName("aws-test")

    with pytest.raises(ProviderEmptyError):
        AwsProviderBackend.build_provider_instance(name=name, config=config, mngr_ctx=temp_mngr_ctx)

    assert len(log_warnings) == 1, f"expected exactly one warning, got {log_warnings!r}"
    assert "aws-test" in log_warnings[0]
    assert "skipping discovery" in log_warnings[0]


def test_bootstrap_for_host_creation_raises_provider_empty_without_warning_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """The create path surfaces the error directly and emits no discovery warning.

    This is the differentiator from the read paths above: ``mngr create`` calls
    ``bootstrap_for_host_creation`` before ``build_provider_instance``, so the
    error is raised here (cleanly, as the create command's top-level failure)
    and build's read-path warning is never reached.
    """
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_ami_id="ami-deadbeef")
    name = ProviderInstanceName("aws-test")

    with pytest.raises(ProviderEmptyError):
        AwsProviderBackend.bootstrap_for_host_creation(name=name, config=config, mngr_ctx=temp_mngr_ctx)

    assert log_warnings == [], f"create path must not emit a discovery warning, got {log_warnings!r}"


def test_bootstrap_for_host_creation_raises_provider_empty_without_warning_when_no_ami_configured(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """The second failure mode (no usable AMI) also surfaces silently on the create path."""
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    config = AwsProviderConfig(
        backend=AWS_BACKEND_NAME,
        default_ami_id="",
        default_ami_by_region={},
    )
    name = ProviderInstanceName("aws-test")

    with pytest.raises(ProviderEmptyError):
        AwsProviderBackend.bootstrap_for_host_creation(name=name, config=config, mngr_ctx=temp_mngr_ctx)

    assert log_warnings == [], f"create path must not emit a discovery warning, got {log_warnings!r}"


def test_bootstrap_for_host_creation_succeeds_quietly_when_credentials_and_ami_resolve(
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """When credentials + AMI resolve, bootstrap is a quiet no-op (no raise, no warn)."""
    clear_aws_env(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    config = AwsProviderConfig(backend=AWS_BACKEND_NAME, default_ami_id="ami-deadbeef")

    AwsProviderBackend.bootstrap_for_host_creation(
        name=ProviderInstanceName("aws-test"), config=config, mngr_ctx=temp_mngr_ctx
    )

    assert log_warnings == []
