import json
import tomllib
from pathlib import Path
from typing import Final

import pytest
from pydantic import AnyUrl

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.minds.config.data_types import OriginsConfig
from imbue.minds.config.data_types import PlanQuotasConfig
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.config.data_types import parse_agents_from_mngr_output
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.errors import MalformedMngrOutputError
from imbue.mngr.primitives import AgentId


def test_workspace_paths_workspace_dir_uses_agent_id(tmp_path: Path) -> None:
    """Verify workspace_dir incorporates the agent_id into the path."""
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId()

    result = paths.workspace_dir(agent_id)
    assert result.parent == tmp_path
    assert str(agent_id) in str(result)


def test_workspace_paths_auth_dir_is_under_data_dir(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    assert paths.auth_dir == tmp_path / "auth"


def test_workspace_paths_mngr_host_dir_is_under_data_dir(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    assert paths.mngr_host_dir == tmp_path / "mngr"


# -- parse_agents_from_mngr_output tests --


def test_parse_agents_from_mngr_output_extracts_records() -> None:
    """Verify parse_agents_from_mngr_output extracts agent records from JSON."""
    json_str = json.dumps(
        {
            "agents": [
                {"id": "agent-abc123", "name": "selene", "work_dir": "/tmp/minds/selene"},
            ]
        }
    )
    agents = parse_agents_from_mngr_output(json_str)
    assert len(agents) == 1
    assert agents[0]["id"] == "agent-abc123"
    assert agents[0]["name"] == "selene"


def test_parse_agents_from_mngr_output_handles_empty() -> None:
    """Verify parse_agents_from_mngr_output returns empty list for no agents."""
    json_str = json.dumps({"agents": []})
    agents = parse_agents_from_mngr_output(json_str)
    assert agents == []


def test_parse_agents_from_mngr_output_raises_on_non_json() -> None:
    """Non-JSON output is treated as a real upstream bug rather than soft-failed."""
    with pytest.raises(MalformedMngrOutputError, match="Expected JSON object"):
        parse_agents_from_mngr_output("not json at all")


def test_parse_agents_from_mngr_output_raises_on_mixed_output() -> None:
    """stdout is reserved for JSON; if a log/warning leaks onto stdout the upstream is broken."""
    output = "WARNING: some SSH error\n" + json.dumps({"agents": [{"id": "agent-xyz", "name": "test"}]})
    with pytest.raises(MalformedMngrOutputError, match="Expected JSON object"):
        parse_agents_from_mngr_output(output)


def test_parse_agents_from_mngr_output_raises_on_invalid_json_first_line() -> None:
    """A line that starts with '{' but isn't valid JSON surfaces as JSONDecodeError."""
    valid_json = json.dumps({"agents": [{"id": "agent-abc", "name": "test"}]})
    output = "{invalid json here\n" + valid_json
    with pytest.raises(json.JSONDecodeError):
        parse_agents_from_mngr_output(output)


@pytest.mark.parametrize("stdout", ["", "   ", "\n\n", "   \n  \n"])
def test_parse_agents_from_mngr_output_raises_on_empty_stdout(stdout: str) -> None:
    """Empty/blank stdout means mngr produced no output at all, not "no agents"."""
    with pytest.raises(MalformedMngrOutputError, match="stdout was empty/blank"):
        parse_agents_from_mngr_output(stdout)


def test_parse_agents_from_mngr_output_raises_on_missing_agents_key() -> None:
    """A JSON object lacking an 'agents' key is malformed output, not a bare KeyError."""
    output = json.dumps({"not_agents": []})
    with pytest.raises(MalformedMngrOutputError, match="missing 'agents' key"):
        parse_agents_from_mngr_output(output)


def test_plan_quotas_config_to_plan_row_converts_gb_to_bytes() -> None:
    config = PlanQuotasConfig(
        max_remote_workspaces=NonNegativeInt(2),
        max_total_workspaces=NonNegativeInt(10),
        max_buckets=NonNegativeInt(5),
        max_total_bucket_gb=NonNegativeInt(50),
        monthly_llm_spend_usd=NonNegativeFloat(0),
        max_active_synced_workspaces=NonNegativeInt(200),
    )
    row = config.to_plan_row()
    assert row["max_total_bucket_bytes"] == 50 * 1024**3
    assert row["monthly_llm_spend_usd"] == 0.0
    assert row["max_remote_workspaces"] == 2
    # Every quota column the connector's plans table carries is present.
    assert row["max_total_workspaces"] == 10
    assert sorted(row) == [
        "max_active_synced_workspaces",
        "max_buckets",
        "max_remote_workspaces",
        "max_total_bucket_bytes",
        "max_total_workspaces",
        "monthly_llm_spend_usd",
    ]


_EXPECTED_DEPLOY_TIERS: Final[frozenset[str]] = frozenset({"ci", "dev", "staging", "production"})


def test_committed_deploy_tomls_all_define_the_launch_plans() -> None:
    """Every tier ships the exact same plan definitions (per-user bumps handle exceptions).

    Tiers are discovered from disk (every ``envs/*/deploy.toml``) rather than
    hardcoded, so a newly-added tier cannot silently diverge; the known four
    are asserted present so a renamed tier cannot drop out of coverage.
    """
    envs_dir = Path(__file__).parent / "envs"
    deploy_paths = sorted(envs_dir.glob("*/deploy.toml"))
    discovered_tiers = {path.parent.name for path in deploy_paths}
    assert _EXPECTED_DEPLOY_TIERS <= discovered_tiers, (
        f"missing deploy.toml for tiers: {sorted(_EXPECTED_DEPLOY_TIERS - discovered_tiers)}"
    )
    plan_blocks_by_tier: dict[str, dict[str, PlanQuotasConfig]] = {}
    for path in deploy_paths:
        raw = tomllib.loads(path.read_text())
        plans = {name: PlanQuotasConfig.model_validate(values) for name, values in raw.get("plans", {}).items()}
        plan_blocks_by_tier[path.parent.name] = plans
    for tier, plans in plan_blocks_by_tier.items():
        assert sorted(plans) == ["ally", "explorer", "free"], f"tier {tier} is missing a launch plan"
        assert plans == plan_blocks_by_tier["dev"], f"tier {tier} diverges from the shared [plans] values"
    assert plan_blocks_by_tier["dev"]["free"].max_remote_workspaces == 1
    assert plan_blocks_by_tier["dev"]["free"].monthly_llm_spend_usd == 0.0
    assert plan_blocks_by_tier["dev"]["explorer"].monthly_llm_spend_usd == 0.0
    assert plan_blocks_by_tier["dev"]["ally"].monthly_llm_spend_usd == 1000.0


def test_origins_config_accepts_https_subdomains_of_the_cookie_domain() -> None:
    origins = OriginsConfig(
        accounts_origin=AnyUrl("https://accounts.imbue-staging.com"),
        chrome_origin=AnyUrl("https://minds.imbue-staging.com"),
        cookie_domain=NonEmptyStr("imbue-staging.com"),
    )
    assert origins.accounts_origin.host == "accounts.imbue-staging.com"
    assert origins.chrome_origin.host == "minds.imbue-staging.com"


def test_origins_config_rejects_non_https_origins() -> None:
    with pytest.raises(ValueError, match="must be https"):
        OriginsConfig(
            accounts_origin=AnyUrl("http://accounts.imbue-staging.com"),
            chrome_origin=AnyUrl("https://minds.imbue-staging.com"),
            cookie_domain=NonEmptyStr("imbue-staging.com"),
        )


def test_origins_config_rejects_a_host_outside_the_cookie_domain() -> None:
    with pytest.raises(ValueError, match="not a subdomain"):
        OriginsConfig(
            accounts_origin=AnyUrl("https://accounts.imbue-staging.com"),
            chrome_origin=AnyUrl("https://minds.somewhere-else.com"),
            cookie_domain=NonEmptyStr("imbue-staging.com"),
        )


def test_origins_config_rejects_an_origin_with_a_path() -> None:
    with pytest.raises(ValueError, match="bare origin"):
        OriginsConfig(
            accounts_origin=AnyUrl("https://accounts.imbue-staging.com/login"),
            chrome_origin=AnyUrl("https://minds.imbue-staging.com"),
            cookie_domain=NonEmptyStr("imbue-staging.com"),
        )


def test_committed_tier_deploy_tomls_parse_with_their_origins_blocks() -> None:
    """The committed staging/production deploy.toml [origins] blocks must load
    (and their hosts must sit under each tier's own cookie apex)."""
    for tier, apex in (("staging", "imbue-staging.com"), ("production", "imbue.com")):
        config = load_deploy_config(tier)
        assert config.origins is not None
        assert str(config.origins.cookie_domain) == apex
