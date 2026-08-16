"""Unit tests for the per-env Modal Secret override computation.

The full deploy flow's behaviour is exercised by
``provisioning_test.py``; this file pins the contract that
:func:`compute_per_env_overrides` returns BOTH ``neon.DATABASE_URL`` and
``litellm.DATABASE_URL`` overrides (the former for the connector's
pool-host queries, the latter for the LiteLLM proxy's Prisma-managed
backing store). Both DSNs come from the same per-env Neon project.
"""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

import imbue.minds.config
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.envs.per_env_deploy import _modal_profile_token_workspace
from imbue.minds.envs.per_env_deploy import _select_deployed_app_id
from imbue.minds.envs.per_env_deploy import _service_template_path
from imbue.minds.envs.per_env_deploy import _validate_required_service_values
from imbue.minds.envs.per_env_deploy import _verify_image_requirements_fresh
from imbue.minds.envs.per_env_deploy import build_per_env_secret_values
from imbue.minds.envs.per_env_deploy import compute_per_env_overrides
from imbue.minds.envs.per_env_deploy import find_missing_template_keys
from imbue.minds.envs.per_env_deploy import modal_token_reprovision_hint
from imbue.minds.envs.per_env_deploy import modal_token_workspace_mismatch_message
from imbue.minds.envs.per_env_deploy import parse_template_declared_keys
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import SecretTemplateValidationError
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.providers.neon_db import NeonProjectRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord


def _fake_neon_record() -> NeonProjectRecord:
    return NeonProjectRecord(
        project_id="proj-fake-123",
        project_name="minds-dev-josh",
        branch_id="branch-1",
        host_pool_dsn=SecretStr("postgresql://owner:pw@pooler/host_pool"),
        litellm_cost_dsn=SecretStr("postgresql://owner:pw@pooler/litellm_cost"),
    )


def _fake_supertokens_record() -> SuperTokensAppRecord:
    return SuperTokensAppRecord(
        app_id="dev-josh",
        connection_uri="https://core.example.com/appid-dev-josh",
        api_key=SecretStr("st-api-key"),
    )


def test_compute_per_env_overrides_includes_both_dsn_overrides() -> None:
    """Both neon and litellm services get DSN overrides from the per-env project."""
    overrides = compute_per_env_overrides(
        DevEnvName("dev-josh"),
        modal_workspace="minds-dev",
        tier="dev",
        neon_record=_fake_neon_record(),
        supertokens_record=_fake_supertokens_record(),
    )

    assert overrides["neon"] == {"DATABASE_URL": "postgresql://owner:pw@pooler/host_pool"}
    assert overrides["litellm"] == {"DATABASE_URL": "postgresql://owner:pw@pooler/litellm_cost"}


def test_compute_per_env_overrides_does_not_override_unrelated_services() -> None:
    """Services other than neon / litellm / supertokens / litellm-connector are untouched.

    The dev-tier deploy reads tier-shared values for everything else
    (``cloudflare``, ``pool-ssh``) straight from Vault. The override
    dict only exists for keys we genuinely need to rewrite at deploy
    time.
    """
    overrides = compute_per_env_overrides(
        DevEnvName("dev-josh"),
        modal_workspace="minds-dev",
        tier="dev",
        neon_record=_fake_neon_record(),
        supertokens_record=_fake_supertokens_record(),
    )
    assert set(overrides.keys()) == {"supertokens", "neon", "litellm", "litellm-connector"}


def test_verify_image_requirements_fresh_passes_for_committed_exports() -> None:
    """The committed exports must match uv.lock, so the preflight passes on a clean checkout."""
    with ConcurrencyGroup(name="image-requirements-fresh-test") as cg:
        _verify_image_requirements_fresh("remote-service-connector", cg)
        _verify_image_requirements_fresh("modal-litellm", cg)


def test_verify_image_requirements_fresh_raises_when_export_is_missing() -> None:
    with ConcurrencyGroup(name="image-requirements-missing-test") as cg:
        with pytest.raises(ModalDeployError):
            _verify_image_requirements_fresh("no-such-package", cg)


def test_select_deployed_app_id_matches_description_name_shape() -> None:
    # Regression: `modal app list --json` reports the app name under "Description"
    # (not "Name"). A matcher that only checks Name/name/App finds nothing, which
    # makes the rollback container-termination silently no-op.
    rows: list[object] = [
        {"App ID": "ap-llm", "Description": "llm-ci", "State": "deployed", "Tasks": "0"},
        {"App ID": "ap-rsc", "Description": "rsc-ci", "State": "deployed", "Tasks": "1"},
    ]
    assert _select_deployed_app_id(rows, "llm-ci") == "ap-llm"
    assert _select_deployed_app_id(rows, "rsc-ci") == "ap-rsc"


def test_select_deployed_app_id_skips_stopped_app() -> None:
    rows: list[object] = [
        {"App ID": "ap-old", "Description": "llm-ci", "State": "stopped"},
        {"App ID": "ap-new", "Description": "llm-ci", "State": "deployed"},
    ]
    assert _select_deployed_app_id(rows, "llm-ci") == "ap-new"


def test_select_deployed_app_id_returns_none_when_absent_or_empty() -> None:
    rows: list[object] = [{"App ID": "ap-rsc", "Description": "rsc-ci", "State": "deployed"}]
    assert _select_deployed_app_id(rows, "llm-ci") is None
    assert _select_deployed_app_id([], "llm-ci") is None


def test_modal_profile_token_workspace_reads_bound_workspace() -> None:
    # The `minds-dev` profile's token is actually bound to `imbue` -- the misroute.
    rows: list[object] = [
        {"name": "imbue", "workspace": "imbue", "active": False},
        {"name": "minds-dev", "workspace": "imbue", "active": True},
    ]
    assert _modal_profile_token_workspace(rows, "minds-dev") == "imbue"
    assert _modal_profile_token_workspace(rows, "imbue") == "imbue"


def test_modal_profile_token_workspace_returns_none_when_absent_or_malformed() -> None:
    # Profile not listed.
    assert _modal_profile_token_workspace([{"name": "imbue", "workspace": "imbue"}], "minds-dev") is None
    # Non-dict rows are skipped.
    assert _modal_profile_token_workspace([42, "nope"], "minds-dev") is None
    # Row present but missing / empty workspace.
    assert _modal_profile_token_workspace([{"name": "minds-dev"}], "minds-dev") is None
    assert _modal_profile_token_workspace([{"name": "minds-dev", "workspace": ""}], "minds-dev") is None


def test_modal_token_reprovision_hint_names_the_profile() -> None:
    hint = modal_token_reprovision_hint("minds-dev")
    assert "modal token new --profile minds-dev" in hint
    assert "'minds-dev'" in hint


def test_modal_token_workspace_mismatch_message_flags_wrong_workspace() -> None:
    message = modal_token_workspace_mismatch_message("minds-dev", "imbue")
    assert message is not None
    assert "'imbue'" in message
    assert "'minds-dev'" in message
    assert "modal token new --profile minds-dev" in message


def test_modal_token_workspace_mismatch_message_none_when_matching_or_undetermined() -> None:
    # Workspaces match -> no problem.
    assert modal_token_workspace_mismatch_message("minds-dev", "minds-dev") is None
    # Binding couldn't be determined (best-effort skip) -> no problem.
    assert modal_token_workspace_mismatch_message("minds-dev", None) is None


def test_parse_template_declared_keys_extracts_export_lines() -> None:
    template_text = (
        "# Comment about the service\n"
        "export FRPS_AUTH_SECRET=\n"
        "export SHARE_CONTENT_DOMAIN=minds-example.com\n"
        "# export COMMENTED_OUT=\n"
        "not_an_export=1\n"
        "export BROKER_JWT_SIGNING_KEY_PEM=\n"
    )
    assert parse_template_declared_keys(template_text) == (
        "FRPS_AUTH_SECRET",
        "SHARE_CONTENT_DOMAIN",
        "BROKER_JWT_SIGNING_KEY_PEM",
    )


def test_find_missing_template_keys_reports_only_absent_keys() -> None:
    declared = ("A_KEY", "B_KEY", "C_KEY")
    assert find_missing_template_keys(declared, {"A_KEY", "C_KEY"}) == ("B_KEY",)
    assert find_missing_template_keys(declared, {"A_KEY", "B_KEY", "C_KEY"}) == ()


def test_validate_required_service_values_raises_naming_the_missing_keys() -> None:
    """Validation runs against the real committed sharing template, so drift in
    either direction (schema vs deploy check) surfaces here."""
    template_keys = parse_template_declared_keys(_service_template_path("sharing").read_text())
    assert len(template_keys) > 2
    complete_values = {key: "" for key in template_keys}
    # Complete-but-empty passes: empty means declared-but-unset by design.
    _validate_required_service_values("sharing", complete_values)

    partial_values = dict(complete_values)
    del partial_values["FRPS_AUTH_SECRET"]
    with pytest.raises(SecretTemplateValidationError, match="FRPS_AUTH_SECRET"):
        _validate_required_service_values("sharing", partial_values)


def test_validate_required_service_values_raises_for_unknown_service_template() -> None:
    with pytest.raises(SecretTemplateValidationError, match="template schema"):
        _validate_required_service_values("no-such-service-73194", {})


def test_every_committed_declared_service_has_a_template_schema() -> None:
    """Every service a committed deploy.toml declares must have a `.minds/template/<service>.sh` schema.

    Shared-tier deploys hard-fail on a declared service with no template, so a
    services-list/template mismatch must surface here rather than mid-rollout.
    Tiers are discovered from disk (every ``envs/*/deploy.toml``) so a new tier
    cannot silently escape the check; the known four are asserted present.
    """
    envs_dir = Path(imbue.minds.config.__file__).parent / "envs"
    deploy_paths = sorted(envs_dir.glob("*/deploy.toml"))
    discovered_tiers = {path.parent.name for path in deploy_paths}
    assert {"ci", "dev", "staging", "production"} <= discovered_tiers, (
        f"missing deploy.toml for tiers: {sorted({'ci', 'dev', 'staging', 'production'} - discovered_tiers)}"
    )
    for tier in sorted(discovered_tiers):
        for service in load_deploy_config(tier).secrets.services:
            template_path = _service_template_path(str(service))
            assert template_path.is_file(), (
                f"tier {tier!r} declares service {service!r} in [secrets].services, "
                f"but there is no template schema at {template_path}"
            )


def _install_failing_fake_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fake ``vault`` CLI on PATH that fails every invocation with a transient-style error (exit 1)."""
    fake_vault = tmp_path / "vault"
    fake_vault.write_text("#!/usr/bin/env bash\necho 'permission denied' >&2\nexit 1\n")
    fake_vault.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_build_per_env_secret_values_required_propagates_vault_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A required service's failed Vault read aborts instead of degrading to a placeholder."""
    _install_failing_fake_vault(tmp_path, monkeypatch)

    with ConcurrencyGroup(name="required-vault-failure-test") as cg:
        with pytest.raises(VaultReadError):
            build_per_env_secret_values(
                "sharing",
                tier_vault_prefix="secrets/minds/staging",
                overrides={},
                is_required=True,
                parent_cg=cg,
            )


def test_build_per_env_secret_values_optional_returns_overrides_on_vault_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_failing_fake_vault(tmp_path, monkeypatch)

    with ConcurrencyGroup(name="optional-vault-failure-test") as cg:
        values = build_per_env_secret_values(
            "sharing",
            tier_vault_prefix="secrets/minds/staging",
            overrides={"ACCOUNTS_BASE_URL": "https://accounts.example.com"},
            is_required=False,
            parent_cg=cg,
        )
    assert values == {"ACCOUNTS_BASE_URL": "https://accounts.example.com"}
