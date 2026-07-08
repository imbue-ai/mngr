"""Tests for the pure VPS build-arg parsing helpers."""

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import extract_presence_flag
from imbue.mngr_vps.build_args import parse_vps_build_args

_DEFAULT_REGION = "ewr"
_DEFAULT_PLAN = "vc2-1c-1gb"


def _parse_with_vultr_defaults(build_args: list[str] | None) -> ParsedVpsBuildOptions:
    """Most parser tests run under the Vultr prefix because it's the simplest case."""
    return parse_vps_build_args(
        build_args,
        provider_prefix="vultr",
        default_region=_DEFAULT_REGION,
        default_plan=_DEFAULT_PLAN,
        plan_arg_name="plan",
    )


def test_parse_build_args_defaults_when_none() -> None:
    parsed = _parse_with_vultr_defaults(None)
    assert parsed.region == "ewr"
    assert parsed.plan == "vc2-1c-1gb"
    assert parsed.docker_build_args == ()
    assert parsed.git_depth is None


def test_parse_build_args_defaults_when_empty() -> None:
    parsed = _parse_with_vultr_defaults([])
    assert parsed.region == "ewr"
    assert parsed.plan == "vc2-1c-1gb"
    assert parsed.docker_build_args == ()


def test_parse_build_args_vultr_region() -> None:
    parsed = _parse_with_vultr_defaults(["--vultr-region=lax"])
    assert parsed.region == "lax"
    assert parsed.plan == "vc2-1c-1gb"
    assert parsed.docker_build_args == ()


def test_parse_build_args_vultr_plan() -> None:
    parsed = _parse_with_vultr_defaults(["--vultr-plan=vc2-2c-4gb"])
    assert parsed.plan == "vc2-2c-4gb"


def test_parse_build_args_docker_args_passthrough() -> None:
    parsed = _parse_with_vultr_defaults(["--file=Dockerfile", "."])
    assert parsed.region == "ewr"
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_mixed_vps_and_docker() -> None:
    parsed = _parse_with_vultr_defaults(
        ["--vultr-plan=vc2-2c-4gb", "--file=Dockerfile", "--vultr-region=lax", "."],
    )
    assert parsed.region == "lax"
    assert parsed.plan == "vc2-2c-4gb"
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_all_vultr_overrides() -> None:
    parsed = _parse_with_vultr_defaults(
        ["--vultr-region=sjc", "--vultr-plan=vc2-4c-8gb"],
    )
    assert parsed.region == "sjc"
    assert parsed.plan == "vc2-4c-8gb"
    assert parsed.docker_build_args == ()


def test_parse_build_args_rejects_unknown_vultr_arg() -> None:
    with pytest.raises(MngrError, match="Unknown vultr build arg.*--vultr-regiom"):
        _parse_with_vultr_defaults(["--vultr-regiom=ewr"])


def test_parse_build_args_rejects_dropped_vps_prefix_with_migration_hint() -> None:
    """The old shared --vps-* prefix raises a migration error pointing at the new per-provider name."""
    with pytest.raises(MngrError, match="no longer supported.*--vultr-region=.*--vultr-plan="):
        _parse_with_vultr_defaults(["--vps-region=ewr"])


def test_parse_build_args_rejects_dropped_vps_os_arg() -> None:
    """--vps-os= used to override the Vultr OS id / OVH image name; now rejected with a guiding error.

    The error must mention the per-provider config field that replaced it
    (default_os_id / default_image_name / default_ami_id), not just say
    "unknown arg".
    """
    with pytest.raises(MngrError, match="no longer supported.*default_os_id.*default_image_name.*default_ami_id"):
        _parse_with_vultr_defaults(["--vps-os=9999"])


def test_parse_build_args_rejects_vps_image_arg_with_guidance() -> None:
    """The dedicated error also catches a plausible alternative spelling (--vps-image=)."""
    with pytest.raises(MngrError, match="no longer supported"):
        _parse_with_vultr_defaults(["--vps-image=debian-12"])


def test_parse_build_args_rejects_vps_ami_arg_with_guidance() -> None:
    """And catches the AWS-flavoured spelling (--vps-ami=)."""
    with pytest.raises(MngrError, match="no longer supported"):
        _parse_with_vultr_defaults(["--vps-ami=ami-0123abcd"])


def test_parse_build_args_git_depth() -> None:
    parsed = _parse_with_vultr_defaults(["--git-depth=1", "--file=Dockerfile", "."])
    assert parsed.git_depth == 1
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_non_integer_git_depth_raises_mngr_error() -> None:
    with pytest.raises(MngrError, match="--git-depth must be an integer.*'abc'"):
        _parse_with_vultr_defaults(["--git-depth=abc"])


def test_parse_build_args_aws_prefix_uses_instance_type_arg_name() -> None:
    """When provider_prefix='aws' and plan_arg_name='instance-type', --aws-instance-type= drives plan."""
    parsed = parse_vps_build_args(
        ["--aws-region=us-east-1", "--aws-instance-type=t3.medium"],
        provider_prefix="aws",
        default_region="us-west-2",
        default_plan="t3.small",
        plan_arg_name="instance-type",
    )
    assert parsed.region == "us-east-1"
    assert parsed.plan == "t3.medium"


def test_parse_build_args_aws_rejects_aws_plan() -> None:
    """`--aws-plan=` is not the AWS arg name; it's `--aws-instance-type=`. The error should be specific."""
    with pytest.raises(MngrError, match="Unknown aws build arg.*--aws-plan"):
        parse_vps_build_args(
            ["--aws-plan=t3.medium"],
            provider_prefix="aws",
            default_region="us-east-1",
            default_plan="t3.small",
            plan_arg_name="instance-type",
        )


# =============================================================================
# extract_presence_flag (composable helper for boolean opt-in flags)
# =============================================================================


def test_extract_presence_flag_returns_false_when_absent() -> None:
    """Default behavior: no occurrence -> (False, args verbatim)."""
    present, remaining = extract_presence_flag(["--file=Dockerfile", "."], "--aws-spot")
    assert present is False
    assert remaining == ["--file=Dockerfile", "."]


def test_extract_presence_flag_returns_true_and_strips_when_present() -> None:
    """Bare flag occurrence -> (True, args with flag removed)."""
    present, remaining = extract_presence_flag(
        ["--file=Dockerfile", "--aws-spot", "."],
        "--aws-spot",
    )
    assert present is True
    assert remaining == ["--file=Dockerfile", "."]


def test_extract_presence_flag_rejects_value_bearing_form() -> None:
    """``--aws-spot=anything`` -> error (clearer than silently accepting either form)."""
    with pytest.raises(MngrError, match="presence-only flag"):
        extract_presence_flag(["--aws-spot=true"], "--aws-spot")
