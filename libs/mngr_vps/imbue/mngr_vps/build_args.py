from collections.abc import Sequence
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError


class ParsedVpsBuildOptions(FrozenModel):
    """Result of parsing VPS-specific build args from Docker build args."""

    region: str = Field(description="VPS region")
    plan: str = Field(description="VPS plan")
    git_depth: int | None = Field(
        default=None, description="Git clone depth for build context, or None for full clone"
    )
    docker_build_args: tuple[str, ...] = Field(description="Remaining args passed to docker build")


def extract_single_value_arg(args: Sequence[str], flag: str) -> tuple[str | None, list[str]]:
    """Pop ``--flag=VALUE`` once from ``args``. Returns ``(value or None, remaining)``.

    If ``flag`` appears multiple times the last occurrence wins (matching how
    docker's CLI treats repeated single-value flags). Composable building
    block: each provider's ``_parse_build_args`` chains a few of these to
    peel off its own knobs before walking the remainder for docker forwarding.
    """
    value: str | None = None
    remaining: list[str] = []
    for arg in args:
        if arg.startswith(flag):
            value = arg.split("=", 1)[1]
        else:
            remaining.append(arg)
    return value, remaining


def extract_git_depth(args: Sequence[str]) -> tuple[int | None, list[str]]:
    """Pop ``--git-depth=N`` from ``args``. Shared because it's about the *local*
    mngr build context (shallow-cloning the upload tarball), not the VPS, so
    every provider accepts it under the same name.
    """
    raw, remaining = extract_single_value_arg(args, "--git-depth=")
    if raw is None:
        return None, remaining
    try:
        return int(raw), remaining
    except ValueError as e:
        raise MngrError(f"--git-depth must be an integer. Got: {raw!r}") from e


def extract_presence_flag(args: Sequence[str], flag: str) -> tuple[bool, list[str]]:
    """Pop a presence-only flag like ``--aws-spot`` from ``args``.

    Returns ``(True, remaining)`` if any element of ``args`` equals ``flag``
    exactly, else ``(False, args_as_list)``. The flag MUST be passed with no
    value: ``--aws-spot=true`` or ``--aws-spot=`` raises because that shape
    suggests the caller expected a value-bearing flag (likely a typo).

    Composable building block for boolean opt-in knobs (e.g. ``--aws-spot``).
    """
    present = False
    remaining: list[str] = []
    for arg in args:
        if arg == flag:
            present = True
        elif arg.startswith(f"{flag}="):
            raise MngrError(f"{flag} is a presence-only flag; pass it as bare {flag!r} (no value). Got: {arg!r}")
        else:
            remaining.append(arg)
    return present, remaining


_VPS_MIGRATION_HINT: Final[str] = (
    "Build args are now per-provider: use --aws-region= / --aws-instance-type= / --aws-ami=, "
    "--vultr-region= / --vultr-plan=, or --ovh-datacenter= (alias --ovh-region=) / --ovh-plan= "
    "(matching your provider). The old --vps-os= / --vps-image= / --vps-ami= image-selection args "
    "are also removed; image selection lives on the provider config (default_os_id for Vultr, "
    "default_image_name for OVH, default_ami_id for AWS)."
)


def raise_if_vps_migration_arg(arg: str) -> None:
    """Raise the dedicated migration error if ``arg`` uses the dropped shared ``--vps-*`` prefix.

    Called by every provider's parser (and by ``MinimalVpsProvider``)
    so callers still passing ``--vps-region=`` etc. get a clear pointer at
    the new per-provider name rather than having the arg silently forwarded
    to docker (which would either error opaquely or, worse, succeed for a
    flag that happens to be a valid docker flag).
    """
    if arg.startswith("--vps-"):
        raise MngrError(f"{arg.split('=', 1)[0]} is no longer supported. {_VPS_MIGRATION_HINT}")


def raise_if_unknown_provider_arg(arg: str, provider_prefix: str, valid_args: Sequence[str]) -> None:
    """Raise if ``arg`` starts with ``--<provider_prefix>-`` but isn't one of ``valid_args``.

    Lets a provider's parser catch typos / unknown flags up front, with a
    specific error that lists what was actually accepted. ``valid_args``
    should be the full flag spellings (e.g. ``("--aws-region=", ...)``) so
    the error message matches the user-facing names exactly.
    """
    if not arg.startswith(f"--{provider_prefix}-"):
        return
    raise MngrError(f"Unknown {provider_prefix} build arg: {arg}. Valid args: {', '.join(valid_args)}")


def parse_vps_build_args(
    build_args: Sequence[str] | None,
    *,
    provider_prefix: str,
    default_region: str,
    default_plan: str,
    plan_arg_name: str,
) -> ParsedVpsBuildOptions:
    """Convenience parser for the common provider shape (region + plan + git-depth).

    Builds the standard four-step parse out of ``extract_single_value_arg``,
    ``extract_git_depth``, ``raise_if_vps_migration_arg``, and
    ``raise_if_unknown_provider_arg``. Vultr and OVH (which only have a
    region + plan) call this directly; AWS has its own composition because
    it also accepts ``--aws-ami=``. Custom providers with their own knobs
    should compose the helpers directly rather than extending this function.
    """
    args = list(build_args or ())
    region_arg = f"--{provider_prefix}-region="
    plan_arg = f"--{provider_prefix}-{plan_arg_name}="
    region, args = extract_single_value_arg(args, region_arg)
    plan, args = extract_single_value_arg(args, plan_arg)
    git_depth, args = extract_git_depth(args)
    docker_build_args: list[str] = []
    for arg in args:
        raise_if_vps_migration_arg(arg)
        raise_if_unknown_provider_arg(arg, provider_prefix, (region_arg, plan_arg, "--git-depth="))
        docker_build_args.append(arg)
    return ParsedVpsBuildOptions(
        region=region or default_region,
        plan=plan or default_plan,
        git_depth=git_depth,
        docker_build_args=tuple(docker_build_args),
    )
