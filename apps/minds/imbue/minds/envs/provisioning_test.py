from pathlib import Path

import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.config.data_types import DeploySecretsConfig
from imbue.minds.envs.local_store import client_config_exists
from imbue.minds.envs.local_store import env_root_exists
from imbue.minds.envs.local_store import read_client_config_file
from imbue.minds.envs.local_store import read_secrets_file
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError
from imbue.minds.envs.primitives import DevEnvProvisioningError
from imbue.minds.envs.providers.modal_env import ModalEnvProviderError
from imbue.minds.envs.providers.neon_db import NeonDatabaseRecord
from imbue.minds.envs.providers.neon_db import NeonProviderError
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensProviderError
from imbue.minds.envs.providers.vultr_tags import VultrInstanceSummary
from imbue.minds.envs.provisioning import ProviderCredentials
from imbue.minds.envs.provisioning import Providers
from imbue.minds.envs.provisioning import deploy_dev_env
from imbue.minds.envs.provisioning import deploy_tier_env
from imbue.minds.envs.provisioning import destroy_dev_env
from imbue.minds.envs.provisioning import destroy_tier_env
from imbue.minds.envs.provisioning import list_dev_envs
from imbue.minds.primitives import ServiceName


@pytest.fixture
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    return tmp_path


@pytest.fixture
def _root_cg() -> ConcurrencyGroup:
    """Bare ConcurrencyGroup the fakes accept as their parent.

    The fakes never spawn subprocesses, so we don't need to ``with`` it.
    """
    return ConcurrencyGroup(name="provisioning-test-root")


def _deploy_config(*, tier: str = "dev", modal_env: str = "main") -> DeployEnvConfig:
    return DeployEnvConfig(
        modal_workspace=NonEmptyStr("dev-workspace"),
        modal_env=NonEmptyStr(modal_env),
        vault_path_prefix=NonEmptyStr(f"secrets/minds/{tier}"),
        cloudflare_domain=NonEmptyStr(f"{tier}.example.com"),
        secrets=DeploySecretsConfig(services=(ServiceName("cloudflare"),)),
    )


def _credentials() -> ProviderCredentials:
    return ProviderCredentials(
        neon_project_id="proj-123",
        neon_api_token=SecretStr("neon-token"),
        supertokens_core_url="https://supertokens.example.com",
        supertokens_api_key=SecretStr("st-api-key"),
        vultr_api_key=SecretStr("vultr-token"),
    )


def _make_call_log() -> dict[str, list]:
    return {"calls": []}


def _build_fake_providers(
    call_log: dict[str, list],
    *,
    fail_step: str | None = None,
    fail_delete: set[str] | None = None,
    vultr_instances: tuple[VultrInstanceSummary, ...] = (),
) -> Providers:
    fail_delete = fail_delete or set()

    def ensure_modal_env(name, cg):
        call_log["calls"].append(("ensure_modal_env", str(name)))
        if fail_step == "modal_env":
            raise ModalEnvProviderError("modal create boom")

    def delete_modal_env(name, cg):
        call_log["calls"].append(("delete_modal_env", str(name)))
        if "modal_env" in fail_delete:
            raise ModalEnvProviderError("modal delete boom")

    def create_neon_db(name, project_id, api_token):
        call_log["calls"].append(("create_neon_db", str(name)))
        if fail_step == "neon_db":
            raise NeonProviderError("neon create boom")
        return NeonDatabaseRecord(
            project_id=project_id,
            branch_id="branch-1",
            database_name=f"minds-dev-{name}",
            role_name="minds_dev",
            pooled_dsn=SecretStr(f"postgres://pooled/{name}"),
        )

    def delete_neon_db(name, project_id, api_token):
        call_log["calls"].append(("delete_neon_db", str(name)))
        if "neon_db" in fail_delete:
            raise NeonProviderError("neon delete boom")

    def create_supertokens_app(name, core_base_url, api_key):
        call_log["calls"].append(("create_supertokens_app", str(name)))
        if fail_step == "supertokens_app":
            raise SuperTokensProviderError("supertokens create boom")
        return SuperTokensAppRecord(
            app_id=str(name),
            connection_uri=f"{core_base_url}/appid-{name}",
            api_key=api_key,
        )

    def delete_supertokens_app(name, core_base_url, api_key):
        call_log["calls"].append(("delete_supertokens_app", str(name)))
        if "supertokens_app" in fail_delete:
            raise SuperTokensProviderError("supertokens delete boom")

    def list_vultr_instances(name, api_key):
        call_log["calls"].append(("list_vultr_instances", str(name)))
        return vultr_instances

    def delete_vultr_instances(instances, api_key):
        call_log["calls"].append(("delete_vultr_instances", len(instances)))

    def read_per_env_secret_values(service, tier_vault_prefix, overrides, cg):
        call_log["calls"].append(("read_per_env_secret_values", service))
        return dict(overrides)

    def push_per_env_modal_secret(secret_name, values, modal_env, cg):
        call_log["calls"].append(("push_per_env_modal_secret", secret_name, modal_env))
        if fail_step == "push_secret" and "supertokens" in secret_name:
            raise ModalDeployError("push secret boom")

    def deploy_litellm_proxy(modal_env, tier, cg):
        call_log["calls"].append(("deploy_litellm_proxy", modal_env, tier))
        if fail_step == "deploy_litellm":
            raise ModalDeployError("litellm deploy boom")
        return AnyUrl(f"https://fake-litellm-{modal_env}.modal.run")

    def deploy_remote_service_connector(modal_env, tier, cg):
        call_log["calls"].append(("deploy_remote_service_connector", modal_env, tier))
        if fail_step == "deploy_connector":
            raise ModalDeployError("connector deploy boom")
        return AnyUrl(f"https://fake-connector-{modal_env}.modal.run")

    def stop_modal_app(app_name, modal_env, cg):
        call_log["calls"].append(("stop_modal_app", app_name, modal_env))
        if fail_step == "stop_modal_app":
            raise ModalDeployError("modal app stop boom")

    return Providers(
        ensure_modal_env=ensure_modal_env,
        delete_modal_env=delete_modal_env,
        create_neon_db=create_neon_db,
        delete_neon_db=delete_neon_db,
        create_supertokens_app=create_supertokens_app,
        delete_supertokens_app=delete_supertokens_app,
        list_vultr_instances=list_vultr_instances,
        delete_vultr_instances=delete_vultr_instances,
        read_per_env_secret_values=read_per_env_secret_values,
        push_per_env_modal_secret=push_per_env_modal_secret,
        deploy_litellm_proxy=deploy_litellm_proxy,
        deploy_remote_service_connector=deploy_remote_service_connector,
        stop_modal_app=stop_modal_app,
    )


def test_deploy_dev_env_writes_split_files(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Dev deploy must write client.toml (URLs only) + secrets.toml (chmod 600)."""
    providers = _build_fake_providers(_make_call_log())
    result = deploy_dev_env(
        DevEnvName("alice"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    assert str(result.connector_url) == "https://fake-connector-alice.modal.run/"
    assert str(result.litellm_proxy_url) == "https://fake-litellm-alice.modal.run/"

    # client.toml has only URL fields (no secrets).
    assert client_config_exists(DevEnvName("alice"))
    public = read_client_config_file(DevEnvName("alice"))
    assert str(public.connector_url) == "https://fake-connector-alice.modal.run/"
    assert str(public.litellm_proxy_url) == "https://fake-litellm-alice.modal.run/"

    # secrets.toml has the per-env provider state, chmod 600.
    secrets = read_secrets_file(DevEnvName("alice"))
    assert secrets.secrets["NEON_POOLED_DSN"].get_secret_value() == "postgres://pooled/alice"
    assert "SUPERTOKENS_CONNECTION_URI" in secrets.secrets
    assert "SUPERTOKENS_API_KEY" in secrets.secrets

    # Sanity: the result struct carries paths to both files.
    assert result.client_config_path.endswith("/.minds-alice/client.toml")
    assert result.secrets_path.endswith("/.minds-alice/secrets.toml")


def test_deploy_dev_env_is_idempotent_on_re_run(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Re-running deploy for an existing env succeeds (overwrites in place)."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_dev_env(
        DevEnvName("bob"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploy_dev_env(
        DevEnvName("bob"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploy_calls = [
        c for c in call_log["calls"] if c[0] in ("deploy_litellm_proxy", "deploy_remote_service_connector")
    ]
    # Per run: 1 litellm + 2 connector deploys. Two runs => 2 + 4.
    assert len([c for c in deploy_calls if c[0] == "deploy_litellm_proxy"]) == 2
    assert len([c for c in deploy_calls if c[0] == "deploy_remote_service_connector"]) == 4


def test_deploy_dev_env_rollback_on_neon_failure(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Modal env created; Neon failed -> Modal env deleted, no secret push, no app deploy."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log, fail_step="neon_db")
    with pytest.raises(DevEnvProvisioningError, match="neon create boom"):
        deploy_dev_env(
            DevEnvName("carol"),
            tier="dev",
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )
    step_names = [c[0] for c in call_log["calls"]]
    assert step_names == [
        "ensure_modal_env",
        "create_neon_db",
        "delete_modal_env",
    ]
    # No client.toml / secrets.toml written for a failed deploy.
    assert not client_config_exists(DevEnvName("carol"))


def test_deploy_dev_env_rollback_on_supertokens_failure(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log, fail_step="supertokens_app")
    with pytest.raises(DevEnvProvisioningError, match="supertokens create boom"):
        deploy_dev_env(
            DevEnvName("dan"),
            tier="dev",
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )
    step_names = [c[0] for c in call_log["calls"]]
    assert step_names == [
        "ensure_modal_env",
        "create_neon_db",
        "create_supertokens_app",
        "delete_neon_db",
        "delete_modal_env",
    ]


def test_deploy_dev_env_rollback_swallows_secondary_failure(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(
        call_log,
        fail_step="supertokens_app",
        fail_delete={"neon_db"},
    )
    with pytest.raises(DevEnvProvisioningError, match="supertokens create boom"):
        deploy_dev_env(
            DevEnvName("eve"),
            tier="dev",
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )
    step_names = [c[0] for c in call_log["calls"]]
    assert "delete_modal_env" in step_names


def test_deploy_dev_env_pushes_per_env_secrets_into_dev_modal_env(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """On a clean deploy, every per-env push targets the dev env's Modal env."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_dev_env(
        DevEnvName("frank"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    pushes = [c for c in call_log["calls"] if c[0] == "push_per_env_modal_secret"]
    pushed_secret_names = {c[1] for c in pushes}
    assert pushed_secret_names == {
        "litellm-dev",
        "supertokens-dev",
        "cloudflare-dev",
        "neon-dev",
        "pool-ssh-dev",
        "litellm-connector-dev",
        "paid-accounts-dev",
    }
    # All pushes target the same Modal env (the dev env name 'frank') --
    # not the tier's stable 'main' env. Two devs never share one Modal env.
    assert all(c[2] == "frank" for c in pushes)
    deploys = [c for c in call_log["calls"] if c[0].startswith("deploy_")]
    assert deploys == [
        ("deploy_litellm_proxy", "frank", "dev"),
        ("deploy_remote_service_connector", "frank", "dev"),
        ("deploy_remote_service_connector", "frank", "dev"),
    ]


def test_destroy_dev_env_walks_providers_in_reverse_and_removes_root(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_dev_env(
        DevEnvName("george"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    assert env_root_exists(DevEnvName("george"))
    call_log["calls"].clear()

    destroy_dev_env(
        DevEnvName("george"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    step_names = [c[0] for c in call_log["calls"]]
    assert step_names == [
        "list_vultr_instances",
        "delete_supertokens_app",
        "delete_neon_db",
        "delete_modal_env",
    ]
    # Env root removed so subsequent commands fail fast on a dangling
    # activation rather than silently re-creating partial state.
    assert not env_root_exists(DevEnvName("george"))


def test_destroy_deletes_vultr_instances(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    instances = (VultrInstanceSummary(id="i-1"), VultrInstanceSummary(id="i-2"))
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log, vultr_instances=instances)
    deploy_dev_env(
        DevEnvName("hank"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    call_log["calls"].clear()

    destroy_dev_env(
        DevEnvName("hank"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    delete_call = next(c for c in call_log["calls"] if c[0] == "delete_vultr_instances")
    assert delete_call == ("delete_vultr_instances", 2)


def test_destroy_missing_env_raises(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    providers = _build_fake_providers(_make_call_log())
    with pytest.raises(DevEnvNotFoundError):
        destroy_dev_env(
            DevEnvName("ghost"),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )


def test_list_dev_envs_returns_summaries_in_sorted_order(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    providers = _build_fake_providers(_make_call_log())
    deploy_dev_env(
        DevEnvName("ivy"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploy_dev_env(
        DevEnvName("juan"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    summaries = list_dev_envs()
    names = [s.name for s in summaries]
    assert names == ["ivy", "juan"]
    assert all(str(s.connector_url).startswith("https://fake-connector-") for s in summaries)


def test_list_dev_envs_treats_minds_dir_as_production_row(
    _isolated_home: Path,
) -> None:
    """Production lives at ~/.minds/ and shows up in list_dev_envs as 'production'."""
    (_isolated_home / ".minds").mkdir()
    summaries = list_dev_envs()
    assert any(s.name == "production" for s in summaries)
    # Production has no per-env client.toml under its root by design --
    # the URLs live in the in-repo file. So the row carries None for the
    # connector_url and client_config_path.
    prod = next(s for s in summaries if s.name == "production")
    assert prod.connector_url is None
    assert prod.client_config_path is None


def test_list_dev_envs_marks_dev_env_without_client_toml_as_no_client(
    _isolated_home: Path,
) -> None:
    """A freshly-mkdir'd ~/.minds-josh-3/ (no client.toml yet) still shows up."""
    (_isolated_home / ".minds-josh-3").mkdir()
    summaries = list_dev_envs()
    assert [s.name for s in summaries] == ["josh-3"]
    only = summaries[0]
    assert only.connector_url is None
    assert only.client_config_path is None


# ---------- deploy_tier_env (staging / production path) ----------


def test_deploy_tier_env_writes_nothing_to_disk(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Tier deploys never touch the per-env on-disk state."""
    providers = _build_fake_providers(_make_call_log())
    result = deploy_tier_env(
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    assert result.tier == "staging"
    assert result.modal_env == "main"
    # ~/.minds/, ~/.minds-staging/ are not created by tier deploy.
    assert not (_isolated_home / ".minds-staging").exists()
    assert not (_isolated_home / ".minds").exists()


def test_deploy_tier_env_pushes_secrets_into_named_modal_env(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Tier deploys push every declared service straight from Vault (no overrides)."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_tier_env(
        tier="production",
        deploy_config=_deploy_config(tier="production", modal_env="main"),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    pushes = [c for c in call_log["calls"] if c[0] == "push_per_env_modal_secret"]
    # _deploy_config declares only `cloudflare` in its services list.
    pushed_secret_names = {c[1] for c in pushes}
    assert pushed_secret_names == {"cloudflare-production"}
    # All pushes target the tier's stable Modal env, not a per-dev one.
    assert all(c[2] == "main" for c in pushes)


def test_deploy_tier_env_runs_both_modal_deploys(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """litellm-proxy + connector both get deployed -- in that order."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_tier_env(
        tier="staging",
        deploy_config=_deploy_config(tier="staging"),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploys = [c for c in call_log["calls"] if c[0].startswith("deploy_")]
    assert deploys == [
        ("deploy_litellm_proxy", "main", "staging"),
        ("deploy_remote_service_connector", "main", "staging"),
    ]


def test_destroy_tier_env_stops_both_apps_and_removes_env_root(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """Tier destroy stops both Modal apps and rmdir's ~/.minds-<tier>/."""
    # Materialize the env root + a couple of files so we can verify
    # delete_env_root actually removed everything.
    staging_root = _isolated_home / ".minds-staging"
    staging_root.mkdir()
    (staging_root / "client.toml").write_text('connector_url = "x"\nlitellm_proxy_url = "y"\n')

    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    destroy_tier_env(
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )

    # Stops both apps in the tier's Modal env, in deploy order (litellm first).
    stops = [c for c in call_log["calls"] if c[0] == "stop_modal_app"]
    assert stops == [
        ("stop_modal_app", "litellm-proxy-staging", "main"),
        ("stop_modal_app", "remote-service-connector-staging", "main"),
    ]
    # Env root gone -- subsequent activation has to re-create it.
    assert not staging_root.exists()


def test_destroy_tier_env_is_idempotent_when_env_root_missing(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """Destroy must not fail when the env root has already been removed."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    # Env root doesn't exist -- destroy should still stop the apps and
    # return cleanly.
    destroy_tier_env(
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    stops = [c for c in call_log["calls"] if c[0] == "stop_modal_app"]
    assert len(stops) == 2
