from pathlib import Path

import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.config.data_types import DeploySecretsConfig
from imbue.minds.envs.local_store import read_dev_env_file
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
from imbue.minds.envs.provisioning import destroy_dev_env
from imbue.minds.envs.provisioning import list_dev_envs
from imbue.minds.primitives import ServiceName


@pytest.fixture
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MINDS_ROOT_NAME", "tname")
    return tmp_path


@pytest.fixture
def _root_cg() -> ConcurrencyGroup:
    """Bare ConcurrencyGroup the fakes accept as their parent.

    The fakes never spawn subprocesses, so we don't need to ``with`` it.
    """
    return ConcurrencyGroup(name="provisioning-test-root")


def _deploy_config() -> DeployEnvConfig:
    return DeployEnvConfig(
        modal_workspace=NonEmptyStr("dev-workspace"),
        vault_path_prefix=NonEmptyStr("secrets/minds/dev"),
        cloudflare_domain=NonEmptyStr("dev.example.com"),
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
        # Default test fixture: return only the overrides; behave as if
        # Vault is empty. Tests that care can inspect overrides.
        return dict(overrides)

    def push_per_env_modal_secret(secret_name, values, modal_env, cg):
        call_log["calls"].append(("push_per_env_modal_secret", secret_name, modal_env))
        if fail_step == "push_secret" and "supertokens" in secret_name:
            raise ModalDeployError("push secret boom")

    def deploy_litellm_proxy(name, tier, cg):
        call_log["calls"].append(("deploy_litellm_proxy", str(name), tier))
        if fail_step == "deploy_litellm":
            raise ModalDeployError("litellm deploy boom")
        return AnyUrl(f"https://fake-litellm-{name}.modal.run")

    def deploy_remote_service_connector(name, tier, cg):
        call_log["calls"].append(("deploy_remote_service_connector", str(name), tier))
        if fail_step == "deploy_connector":
            raise ModalDeployError("connector deploy boom")
        return AnyUrl(f"https://fake-connector-{name}.modal.run")

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
    )


def test_deploy_dev_env_writes_local_file(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    result = deploy_dev_env(
        DevEnvName("alice"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    # The fake providers return synthesized URLs that include the dev env name.
    assert str(result.connector_url) == "https://fake-connector-alice.modal.run/"
    assert str(result.litellm_proxy_url) == "https://fake-litellm-alice.modal.run/"
    loaded = read_dev_env_file(DevEnvName("alice"))
    assert loaded.secrets["NEON_POOLED_DSN"].get_secret_value() == "postgres://pooled/alice"
    assert "SUPERTOKENS_CONNECTION_URI" in loaded.secrets
    assert "SUPERTOKENS_API_KEY" in loaded.secrets


def test_deploy_dev_env_is_idempotent_on_re_run(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Re-running deploy for an existing env succeeds (no DevEnvAlreadyExists)."""
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
    # Re-run -- should not raise, should re-push secrets + redeploy.
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
    # Per run: 1 litellm deploy + 2 connector deploys (first + post-URL
    # backfill). Two runs => 2 litellm + 4 connector.
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


def test_deploy_dev_env_pushes_per_env_secrets_and_deploys_apps(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """On a clean deploy, expect a secret push per service and both modal deploys."""
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
    # All pushes target the same modal env (the dev env name).
    assert all(c[2] == "frank" for c in pushes)
    deploys = [c for c in call_log["calls"] if c[0].startswith("deploy_")]
    # Litellm first, then connector twice (initial deploy + a redeploy after
    # the second-pass URL-backfill push of supertokens / litellm-connector).
    assert deploys == [
        ("deploy_litellm_proxy", "frank", "dev"),
        ("deploy_remote_service_connector", "frank", "dev"),
        ("deploy_remote_service_connector", "frank", "dev"),
    ]


def test_destroy_dev_env_walks_providers_in_reverse(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
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
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    with pytest.raises(DevEnvNotFoundError):
        destroy_dev_env(
            DevEnvName("ghost"),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )


def test_list_dev_envs_returns_summaries(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
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
    assert [str(s.name) for s in summaries] == ["ivy", "juan"]
    assert all(str(s.connector_url).startswith("https://fake-connector-") for s in summaries)
