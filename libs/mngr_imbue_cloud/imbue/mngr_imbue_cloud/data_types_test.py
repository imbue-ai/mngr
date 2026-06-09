import pytest

from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.data_types import parse_imbue_cloud_build_args
from imbue.mngr_imbue_cloud.primitives import FastMode


def test_lease_attributes_drops_none_fields() -> None:
    attrs = LeaseAttributes(repo_url="https://example.com/repo.git", cpus=2)
    body = attrs.to_request_dict()
    assert body == {"repo_url": "https://example.com/repo.git", "cpus": 2}
    assert "memory_gb" not in body
    assert "gpu_count" not in body


def test_lease_attributes_empty_dict_when_unconstrained() -> None:
    assert LeaseAttributes().to_request_dict() == {}


def test_lease_attributes_includes_zero_values() -> None:
    # gpu_count=0 means "0 GPUs required", which is constraining and must be sent.
    attrs = LeaseAttributes(gpu_count=0)
    assert attrs.to_request_dict() == {"gpu_count": 0}


def test_relaxed_drops_repo_constraints_keeps_resources() -> None:
    attrs = LeaseAttributes(
        repo_url="https://example.com/repo.git",
        repo_branch_or_tag="v1.2.3",
        cpus=4,
        memory_gb=8,
        gpu_count=0,
    )
    relaxed = attrs.relaxed()
    assert relaxed.to_request_dict() == {"cpus": 4, "memory_gb": 8, "gpu_count": 0}


def test_relaxed_empty_when_only_repo_constrained() -> None:
    attrs = LeaseAttributes(repo_branch_or_tag="v1.2.3")
    assert attrs.relaxed().to_request_dict() == {}


def test_parse_build_args_none_uses_default_fast_mode() -> None:
    parsed = parse_imbue_cloud_build_args(None)
    assert parsed.fast_mode == FastMode.PREVENT
    assert parsed.attributes.to_request_dict() == {}
    assert parsed.account_override is None
    assert parsed.passthrough_build_args == ()


def test_parse_build_args_splits_control_lease_and_passthrough() -> None:
    parsed = parse_imbue_cloud_build_args(
        [
            "account=alice@imbue.com",
            "fast_mode=require",
            "repo_branch_or_tag=v1.2.3",
            "cpus=4",
            "--file=Dockerfile",
            ".",
        ]
    )
    assert parsed.account_override == "alice@imbue.com"
    assert parsed.fast_mode == FastMode.REQUIRE
    assert parsed.attributes.to_request_dict() == {"repo_branch_or_tag": "v1.2.3", "cpus": 4}
    assert parsed.passthrough_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_forwards_docker_build_arg_with_equals() -> None:
    # A docker ``--build-arg KEY=VALUE`` form must survive verbatim, not be
    # mistaken for a recognized lease key.
    parsed = parse_imbue_cloud_build_args(["--build-arg=FOO=bar"])
    assert parsed.passthrough_build_args == ("--build-arg=FOO=bar",)
    assert parsed.attributes.to_request_dict() == {}


def test_parse_build_args_rejects_non_integer_cpus() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        parse_imbue_cloud_build_args(["cpus=lots"])


def test_parse_build_args_rejects_unknown_fast_mode() -> None:
    with pytest.raises(ValueError, match="fast_mode"):
        parse_imbue_cloud_build_args(["fast_mode=maybe"])


def test_parse_build_args_rejects_empty_account() -> None:
    with pytest.raises(ValueError, match="account"):
        parse_imbue_cloud_build_args(["account="])


def test_parse_build_args_fast_mode_is_case_insensitive() -> None:
    parsed = parse_imbue_cloud_build_args(["fast_mode=REQUIRE"])
    assert parsed.fast_mode == FastMode.REQUIRE


def test_parse_build_args_parses_region() -> None:
    parsed = parse_imbue_cloud_build_args(["region=US-EAST-VA"])
    assert parsed.region == "US-EAST-VA"
    # The region is top-level, never folded into the attribute filter.
    assert parsed.attributes.to_request_dict() == {}


def test_parse_build_args_region_defaults_to_none() -> None:
    parsed = parse_imbue_cloud_build_args(["cpus=2"])
    assert parsed.region is None


def test_parse_build_args_rejects_unknown_region() -> None:
    with pytest.raises(ValueError, match="region"):
        parse_imbue_cloud_build_args(["region=US-CENTRAL-TX"])


def test_parse_build_args_rejects_empty_region() -> None:
    with pytest.raises(ValueError, match="region"):
        parse_imbue_cloud_build_args(["region="])
