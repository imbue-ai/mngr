from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.config.data_types import DeploySecretsConfig
from imbue.minds.envs.local_store import read_dev_env_file
from imbue.minds.envs.primitives import DevEnvAlreadyExistsError
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
from imbue.minds.envs.provisioning import create_dev_env
from imbue.minds.envs.provisioning import destroy_dev_env
from imbue.minds.envs.provisioning import list_dev_envs
from imbue.minds.primitives import ServiceName


@pytest.fixture
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MINDS_ROOT_NAME", "tname")
    return tmp_path


def _deploy_config() -> DeployEnvConfig:
    return DeployEnvConfig(
        modal_workspace=NonEmptyStr("dev-workspace"),
        modal_env=NonEmptyStr("main"),
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

    def create_modal_env(name):
        call_log["calls"].append(("create_modal_env", str(name)))
        if fail_step == "modal_env":
            raise ModalEnvProviderError("modal create boom")

    def delete_modal_env(name):
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

    return Providers(
        create_modal_env=create_modal_env,
        delete_modal_env=delete_modal_env,
        create_neon_db=create_neon_db,
        delete_neon_db=delete_neon_db,
        create_supertokens_app=create_supertokens_app,
        delete_supertokens_app=delete_supertokens_app,
        list_vultr_instances=list_vultr_instances,
        delete_vultr_instances=delete_vultr_instances,
    )


def test_create_dev_env_writes_local_file(_isolated_home: Path) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    result = create_dev_env(
        DevEnvName("alice"),
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
    )
    assert str(result.connector_url).startswith("https://dev-workspace--remote-service-connector-alice")
    assert str(result.litellm_proxy_url).startswith("https://dev-workspace--litellm-proxy-alice")
    loaded = read_dev_env_file(DevEnvName("alice"))
    assert loaded.secrets["NEON_POOLED_DSN"].get_secret_value() == "postgres://pooled/alice"
    assert "SUPERTOKENS_CONNECTION_URI" in loaded.secrets
    assert "SUPERTOKENS_API_KEY" in loaded.secrets


def test_create_dev_env_rejects_existing_file(_isolated_home: Path) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    create_dev_env(
        DevEnvName("bob"),
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
    )
    with pytest.raises(DevEnvAlreadyExistsError):
        create_dev_env(
            DevEnvName("bob"),
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
        )


def test_create_dev_env_rollback_on_neon_failure(_isolated_home: Path) -> None:
    """Modal env was created; Neon failed -> Modal env should be deleted."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log, fail_step="neon_db")
    with pytest.raises(DevEnvProvisioningError, match="neon create boom"):
        create_dev_env(
            DevEnvName("carol"),
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
        )
    step_names = [c[0] for c in call_log["calls"]]
    assert step_names == [
        "create_modal_env",
        "create_neon_db",
        "delete_modal_env",
    ]


def test_create_dev_env_rollback_on_supertokens_failure(_isolated_home: Path) -> None:
    """Modal + Neon created; SuperTokens failed -> both should be deleted in reverse order."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log, fail_step="supertokens_app")
    with pytest.raises(DevEnvProvisioningError, match="supertokens create boom"):
        create_dev_env(
            DevEnvName("dan"),
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
        )
    step_names = [c[0] for c in call_log["calls"]]
    assert step_names == [
        "create_modal_env",
        "create_neon_db",
        "create_supertokens_app",
        "delete_neon_db",
        "delete_modal_env",
    ]


def test_create_dev_env_rollback_swallows_secondary_failure(_isolated_home: Path) -> None:
    """A rollback step that itself fails is logged, not re-raised."""
    call_log = _make_call_log()
    providers = _build_fake_providers(
        call_log,
        fail_step="supertokens_app",
        fail_delete={"neon_db"},
    )
    with pytest.raises(DevEnvProvisioningError, match="supertokens create boom"):
        create_dev_env(
            DevEnvName("eve"),
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
        )
    step_names = [c[0] for c in call_log["calls"]]
    # delete_neon_db raised -> we still walked on to delete_modal_env.
    assert "delete_modal_env" in step_names


def test_destroy_dev_env_walks_providers_in_reverse(_isolated_home: Path) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    create_dev_env(
        DevEnvName("frank"),
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
    )
    call_log["calls"].clear()

    destroy_dev_env(
        DevEnvName("frank"),
        credentials=_credentials(),
        providers=providers,
    )
    step_names = [c[0] for c in call_log["calls"]]
    assert step_names == [
        "list_vultr_instances",
        "delete_supertokens_app",
        "delete_neon_db",
        "delete_modal_env",
    ]


def test_destroy_deletes_vultr_instances(_isolated_home: Path) -> None:
    instances = (VultrInstanceSummary(id="i-1"), VultrInstanceSummary(id="i-2"))
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log, vultr_instances=instances)
    create_dev_env(
        DevEnvName("gabe"),
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
    )
    call_log["calls"].clear()

    destroy_dev_env(
        DevEnvName("gabe"),
        credentials=_credentials(),
        providers=providers,
    )
    # We should have seen the listing return 2 instances and then a delete call.
    delete_call = next(c for c in call_log["calls"] if c[0] == "delete_vultr_instances")
    assert delete_call == ("delete_vultr_instances", 2)


def test_destroy_missing_env_raises(_isolated_home: Path) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    with pytest.raises(DevEnvNotFoundError):
        destroy_dev_env(
            DevEnvName("ghost"),
            credentials=_credentials(),
            providers=providers,
        )


def test_list_dev_envs_returns_summaries(_isolated_home: Path) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    create_dev_env(
        DevEnvName("hank"),
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
    )
    create_dev_env(
        DevEnvName("ivy"),
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
    )
    summaries = list_dev_envs()
    assert [str(s.name) for s in summaries] == ["hank", "ivy"]
    assert all(str(s.connector_url).startswith("https://dev-workspace--") for s in summaries)
