from collections import defaultdict
from pathlib import Path
from typing import Final

import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.config.data_types import DeployLifecycleConfig
from imbue.minds.config.data_types import DeploySecretsConfig
from imbue.minds.config.data_types import MinContainersConfig
from imbue.minds.config.data_types import ModalEnvStrategy
from imbue.minds.config.data_types import PaidDefaultsConfig
from imbue.minds.config.data_types import ScaledownWindowConfig
from imbue.minds.envs.docker_cleanup import DockerCleanupError
from imbue.minds.envs.local_store import client_config_exists
from imbue.minds.envs.local_store import env_root_exists
from imbue.minds.envs.local_store import read_client_config_file
from imbue.minds.envs.local_store import read_secrets_file
from imbue.minds.envs.mngr_agent_cleanup import MngrAgentCleanupError
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.providers.modal_env import ModalEnvProviderError
from imbue.minds.envs.providers.neon_db import NeonProjectRecord
from imbue.minds.envs.providers.neon_db import NeonProviderError
from imbue.minds.envs.providers.ovh_tags import OvhCredentials
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensProviderError
from imbue.minds.envs.provisioning import ProviderCredentials
from imbue.minds.envs.provisioning import Providers
from imbue.minds.envs.provisioning import deploy_env
from imbue.minds.envs.provisioning import destroy_env
from imbue.minds.envs.provisioning import list_dev_envs
from imbue.minds.envs.recover import RecoverTargetAlreadyExistsError
from imbue.minds.errors import MindError
from imbue.minds.primitives import ServiceName
from imbue.mngr_ovh.iam_tags import IamResource


@pytest.fixture
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Sandbox $HOME + cwd so deploy/destroy/recover write to a tmp tree only.

    Also seeds an ``apps/`` marker so :func:`find_monorepo_root` (called
    by deploy_env to pick the recover-target file location) finds a
    monorepo root under the tmp tree instead of the real repo.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    (tmp_path / "apps").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def _root_cg() -> ConcurrencyGroup:
    """Bare ConcurrencyGroup the fakes accept as their parent.

    The fakes never spawn subprocesses, so we don't need to ``with`` it.
    """
    return ConcurrencyGroup(name="provisioning-test-root")


# Fixed test workspace name used everywhere ``deploy_dev_env`` /
# ``deploy_tier_env`` constructs an expected URL via ``per_env_*_url`` /
# ``tier_*_url``. The fake deploy_litellm_proxy / deploy_remote_service_connector
# below MUST return URLs matching the same formulas so the assertion
# in ``provisioning._assert_deploy_url_matches`` passes.
_TEST_MODAL_WORKSPACE: Final[str] = "test-ws"


_DEV_LIFECYCLE: Final[DeployLifecycleConfig] = DeployLifecycleConfig(
    creates_resources=True,
    modal_env_strategy=ModalEnvStrategy.PER_ENV,
    writes_local_state=True,
    tracks_generation=False,
)

_SHARED_TIER_LIFECYCLE: Final[DeployLifecycleConfig] = DeployLifecycleConfig(
    creates_resources=False,
    modal_env_strategy=ModalEnvStrategy.SHARED,
    writes_local_state=False,
    tracks_generation=True,
)


def _deploy_config(
    *,
    tier: str = "dev",
    modal_env: str = "main",
    min_containers: MinContainersConfig | None = None,
    scaledown_window: ScaledownWindowConfig | None = None,
    paid: PaidDefaultsConfig | None = None,
    lifecycle: DeployLifecycleConfig | None = None,
) -> DeployEnvConfig:
    if lifecycle is None:
        lifecycle = _DEV_LIFECYCLE if tier == "dev" else _SHARED_TIER_LIFECYCLE
    return DeployEnvConfig(
        modal_workspace=NonEmptyStr(_TEST_MODAL_WORKSPACE),
        modal_env=NonEmptyStr(modal_env),
        vault_path_prefix=NonEmptyStr(f"secrets/minds/{tier}"),
        cloudflare_domain=NonEmptyStr(f"{tier}.example.com"),
        secrets=DeploySecretsConfig(services=(ServiceName("cloudflare"),)),
        lifecycle=lifecycle,
        min_containers=min_containers if min_containers is not None else MinContainersConfig(),
        scaledown_window=scaledown_window if scaledown_window is not None else ScaledownWindowConfig(),
        paid=paid if paid is not None else PaidDefaultsConfig(),
    )


def _credentials(*, neon_project_id: str | None = "proj-fake-shared") -> ProviderCredentials:
    return ProviderCredentials(
        neon_org_id="org-fake-123",
        neon_api_token=SecretStr("neon-token"),
        neon_project_id=neon_project_id,
        supertokens_core_url="https://supertokens.example.com",
        supertokens_api_key=SecretStr("st-api-key"),
        ovh_credentials=OvhCredentials(
            application_key=SecretStr("ovh-ak"),
            application_secret=SecretStr("ovh-as"),
            consumer_key=SecretStr("ovh-ck"),
        ),
    )


def _make_call_log() -> dict[str, list]:
    return {"calls": []}


def _build_fake_providers(
    call_log: dict[str, list],
    *,
    fail_step: str | None = None,
    fail_delete: set[str] | None = None,
    ovh_instances: tuple[IamResource, ...] = (),
    vault_responses: dict[str, dict[str, str]] | None = None,
    cloudflare_tunnels: tuple[str, ...] = (),
) -> Providers:
    fail_delete = fail_delete or set()
    # Canned Vault dicts so tier-destroy wipes can find what they need
    # (SUPERTOKENS_CONNECTION_URI / SUPERTOKENS_API_KEY for the
    # SuperTokens wipe; DATABASE_URL for the Neon wipe). Tests that
    # exercise the empty-Vault failure mode pass an explicit empty dict.
    if vault_responses is None:
        vault_responses = {
            "supertokens": {
                "SUPERTOKENS_CONNECTION_URI": "https://st.example.com/appid-staging",
                "SUPERTOKENS_API_KEY": "fake-api-key",
            },
            "neon": {
                "DATABASE_URL": "postgres://user:pass@host/db",
            },
            "cloudflare": {
                "CLOUDFLARE_ACCOUNT_ID": "fake-cf-account",
                "CLOUDFLARE_API_TOKEN": "fake-cf-token",
            },
        }

    def ensure_modal_env(name, cg):
        call_log["calls"].append(("ensure_modal_env", str(name)))
        if fail_step == "modal_env":
            raise ModalEnvProviderError("modal create boom")

    def delete_modal_env(name, cg):
        call_log["calls"].append(("delete_modal_env", str(name)))
        if "modal_env" in fail_delete:
            raise ModalEnvProviderError("modal delete boom")

    def create_neon_project(name, org_id, api_token, cg):
        call_log["calls"].append(("create_neon_project", str(name)))
        if fail_step == "neon_project":
            raise NeonProviderError("neon create boom")
        return NeonProjectRecord(
            project_id=f"proj-fake-{name}",
            project_name=f"minds-{name}",
            branch_id="branch-1",
            host_pool_dsn=SecretStr(f"postgres://pooled/{name}/host_pool"),
            litellm_cost_dsn=SecretStr(f"postgres://pooled/{name}/litellm_cost"),
        )

    def delete_neon_project(name, org_id, api_token):
        call_log["calls"].append(("delete_neon_project", str(name)))
        if "neon_project" in fail_delete:
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

    def list_ovh_instances(name, credentials):
        call_log["calls"].append(("list_ovh_instances", str(name)))
        return ovh_instances

    def delete_ovh_instances(instances, credentials):
        call_log["calls"].append(("delete_ovh_instances", len(instances)))

    def read_per_env_secret_values(service, tier_vault_prefix, overrides, cg):
        call_log["calls"].append(("read_per_env_secret_values", service))
        # Merge canned Vault baseline + caller overrides, mirroring the
        # real ``build_per_env_secret_values`` behaviour. Empty for
        # services the test setup didn't pre-populate.
        merged = dict(vault_responses.get(service, {}))
        merged.update(overrides)
        return merged

    # Tracks secret state across deploy/destroy cycles so the fake's
    # list_modal_secrets can find what was pushed even if call_log was
    # cleared between phases of a test. Real Modal Secrets persist
    # until explicitly deleted; the fake mirrors that.
    pushed_secrets_state: dict[str, set[str]] = defaultdict(set)

    def push_per_env_modal_secret(secret_name, values, modal_env, cg):
        call_log["calls"].append(("push_per_env_modal_secret", secret_name, modal_env))
        if fail_step == "push_secret" and "supertokens" in secret_name:
            raise ModalDeployError("push secret boom")
        pushed_secrets_state[modal_env].add(secret_name)

    def deploy_litellm_proxy(modal_env, tier, min_containers, scaledown_window, deploy_id, strategy, cg):
        call_log["calls"].append(
            ("deploy_litellm_proxy", modal_env, tier, min_containers, scaledown_window, deploy_id, strategy)
        )
        if fail_step == "deploy_litellm":
            raise ModalDeployError("litellm deploy boom")
        # Track the deploy id as the "version" of the deployed app for
        # the matching get_modal_app_latest_version lookups in later runs.
        deployed_app_versions[(modal_env, f"llm-{tier}")] = deploy_id
        # Match the same URL formula ``deploy_env`` uses so the post-
        # deploy URL-match assertion passes for both per-env (dev) and
        # shared (staging/prod) shapes.
        if tier == "dev":
            return AnyUrl(f"https://{_TEST_MODAL_WORKSPACE}-{modal_env}--llm-dev-proxy.modal.run")
        return AnyUrl(f"https://{_TEST_MODAL_WORKSPACE}--llm-{tier}-proxy.modal.run")

    def deploy_remote_service_connector(modal_env, tier, min_containers, scaledown_window, deploy_id, strategy, cg):
        call_log["calls"].append(
            ("deploy_remote_service_connector", modal_env, tier, min_containers, scaledown_window, deploy_id, strategy)
        )
        if fail_step == "deploy_connector":
            raise ModalDeployError("connector deploy boom")
        deployed_app_versions[(modal_env, f"rsc-{tier}")] = deploy_id
        if tier == "dev":
            return AnyUrl(f"https://{_TEST_MODAL_WORKSPACE}-{modal_env}--rsc-dev-api.modal.run")
        return AnyUrl(f"https://{_TEST_MODAL_WORKSPACE}--rsc-{tier}-api.modal.run")

    def stop_modal_app(app_name, modal_env, cg):
        call_log["calls"].append(("stop_modal_app", app_name, modal_env))
        if fail_step == "stop_modal_app":
            raise ModalDeployError("modal app stop boom")

    def delete_modal_secret(secret_name, modal_env, cg):
        call_log["calls"].append(("delete_modal_secret", secret_name, modal_env))
        if fail_step == "delete_modal_secret":
            raise ModalDeployError("modal secret delete boom")
        pushed_secrets_state[modal_env].discard(secret_name)

    def list_modal_secrets(modal_env, cg):
        call_log["calls"].append(("list_modal_secrets", modal_env))
        return tuple(sorted(pushed_secrets_state[modal_env]))

    def apply_pool_hosts_migrations(host_pool_dsn, cg):
        call_log["calls"].append(("apply_pool_hosts_migrations", host_pool_dsn.get_secret_value()))
        return ()

    def seed_paid_list_defaults(host_pool_dsn, domains, emails, cg):
        call_log["calls"].append(
            ("seed_paid_list_defaults", host_pool_dsn.get_secret_value(), tuple(domains), tuple(emails))
        )

    # Tracks deployed app versions across deploy + recover cycles. Lets
    # the fake `get_modal_app_latest_version` return None for the first
    # deploy and the captured pre-deploy id on subsequent calls.
    deployed_app_versions: dict[tuple[str, str], str] = {}

    def get_modal_app_latest_version(app_name, modal_env, cg):
        call_log["calls"].append(("get_modal_app_latest_version", app_name, modal_env))
        return deployed_app_versions.get((modal_env, app_name))

    def rollback_modal_app(app_name, version, modal_env, cg):
        call_log["calls"].append(("rollback_modal_app", app_name, version, modal_env))

    def create_neon_snapshot_branch(project_id, parent_branch_id, name, api_token):
        call_log["calls"].append(("create_neon_snapshot_branch", project_id, parent_branch_id, name))
        return f"snap-{name}"

    def delete_neon_branch(project_id, branch_id, api_token):
        call_log["calls"].append(("delete_neon_branch", project_id, branch_id))

    def resolve_neon_default_branch_id(project_id, api_token):
        call_log["calls"].append(("resolve_neon_default_branch_id", project_id))
        return f"br-default-{project_id}"

    def verify_neon_token_has_restore_scope(project_id, api_token):
        call_log["calls"].append(("verify_neon_token_has_restore_scope", project_id))

    def await_apps_healthy(connector_url, litellm_proxy_url):
        call_log["calls"].append(("await_apps_healthy", str(connector_url), str(litellm_proxy_url)))

    def destroy_mngr_agents(agent_ids, mngr_host_dir, mngr_prefix, cg):
        call_log["calls"].append(("destroy_mngr_agents", tuple(agent_ids), str(mngr_host_dir), mngr_prefix))
        if fail_step == "destroy_mngr_agents":
            raise MngrAgentCleanupError("mngr destroy boom")

    def cleanup_state_container(name, cg):
        call_log["calls"].append(("cleanup_state_container", str(name)))
        if fail_step == "cleanup_state_container":
            raise DockerCleanupError("docker cleanup boom")

    def wipe_supertokens_app_data(app_id, core_base_url, api_key):
        call_log["calls"].append(("wipe_supertokens_app_data", app_id, core_base_url))
        if fail_step == "wipe_supertokens":
            raise SuperTokensProviderError("st wipe boom")

    def wipe_neon_db_schema(dsn, cg):
        call_log["calls"].append(("wipe_neon_db_schema", dsn.get_secret_value()))
        if fail_step == "wipe_neon":
            raise NeonProviderError("neon wipe boom")

    def ensure_generation_id(tier_vault_prefix, cg):
        call_log["calls"].append(("ensure_generation_id", tier_vault_prefix))
        return "fake-generation-id"

    def delete_generation_id(tier_vault_prefix, cg):
        call_log["calls"].append(("delete_generation_id", tier_vault_prefix))

    def list_cloudflare_tunnels_for_env(name, account_id, api_token):
        call_log["calls"].append(("list_cloudflare_tunnels_for_env", str(name), account_id))
        # Default fake: no tunnels. Tests that care set ``cloudflare_tunnels``.
        return cloudflare_tunnels

    def delete_cloudflare_tunnels(tunnel_ids, account_id, api_token):
        call_log["calls"].append(("delete_cloudflare_tunnels", tuple(tunnel_ids)))

    return Providers(
        ensure_modal_env=ensure_modal_env,
        delete_modal_env=delete_modal_env,
        create_neon_project=create_neon_project,
        delete_neon_project=delete_neon_project,
        create_supertokens_app=create_supertokens_app,
        delete_supertokens_app=delete_supertokens_app,
        list_ovh_instances=list_ovh_instances,
        delete_ovh_instances=delete_ovh_instances,
        read_per_env_secret_values=read_per_env_secret_values,
        push_per_env_modal_secret=push_per_env_modal_secret,
        deploy_litellm_proxy=deploy_litellm_proxy,
        deploy_remote_service_connector=deploy_remote_service_connector,
        stop_modal_app=stop_modal_app,
        delete_modal_secret=delete_modal_secret,
        list_modal_secrets=list_modal_secrets,
        apply_pool_hosts_migrations=apply_pool_hosts_migrations,
        seed_paid_list_defaults=seed_paid_list_defaults,
        get_modal_app_latest_version=get_modal_app_latest_version,
        rollback_modal_app=rollback_modal_app,
        create_neon_snapshot_branch=create_neon_snapshot_branch,
        delete_neon_branch=delete_neon_branch,
        resolve_neon_default_branch_id=resolve_neon_default_branch_id,
        verify_neon_token_has_restore_scope=verify_neon_token_has_restore_scope,
        await_apps_healthy=await_apps_healthy,
        destroy_mngr_agents=destroy_mngr_agents,
        cleanup_state_container=cleanup_state_container,
        wipe_supertokens_app_data=wipe_supertokens_app_data,
        wipe_neon_db_schema=wipe_neon_db_schema,
        ensure_generation_id=ensure_generation_id,
        delete_generation_id=delete_generation_id,
        list_cloudflare_tunnels_for_env=list_cloudflare_tunnels_for_env,
        delete_cloudflare_tunnels=delete_cloudflare_tunnels,
    )


def test_deploy_dev_env_writes_split_files(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Dev deploy must write client.toml (URLs only) + secrets.toml (chmod 600)."""
    providers = _build_fake_providers(_make_call_log())
    result = deploy_env(
        DevEnvName("dev-alice"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    assert str(result.connector_url) == f"https://{_TEST_MODAL_WORKSPACE}-dev-alice--rsc-dev-api.modal.run/"
    assert str(result.litellm_proxy_url) == f"https://{_TEST_MODAL_WORKSPACE}-dev-alice--llm-dev-proxy.modal.run/"

    # client.toml has only URL fields (no secrets).
    assert client_config_exists(DevEnvName("dev-alice"))
    public = read_client_config_file(DevEnvName("dev-alice"))
    assert str(public.connector_url) == f"https://{_TEST_MODAL_WORKSPACE}-dev-alice--rsc-dev-api.modal.run/"
    assert str(public.litellm_proxy_url) == f"https://{_TEST_MODAL_WORKSPACE}-dev-alice--llm-dev-proxy.modal.run/"

    # secrets.toml has the per-env provider state, chmod 600.
    secrets = read_secrets_file(DevEnvName("dev-alice"))
    assert secrets.secrets["NEON_HOST_POOL_DSN"].get_secret_value() == "postgres://pooled/dev-alice/host_pool"
    assert secrets.secrets["NEON_LITELLM_DSN"].get_secret_value() == "postgres://pooled/dev-alice/litellm_cost"
    assert "SUPERTOKENS_CONNECTION_URI" in secrets.secrets
    assert "SUPERTOKENS_API_KEY" in secrets.secrets

    # Sanity: the result struct carries paths to both files.
    assert result.client_config_path is not None and result.client_config_path.endswith(
        "/.minds-dev-alice/client.toml"
    )
    assert result.secrets_path is not None and result.secrets_path.endswith("/.minds-dev-alice/secrets.toml")


def test_deploy_dev_env_is_idempotent_on_re_run(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Re-running deploy for an existing env succeeds (overwrites in place)."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("dev-bob"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploy_env(
        DevEnvName("dev-bob"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploy_calls = [
        c for c in call_log["calls"] if c[0] in ("deploy_litellm_proxy", "deploy_remote_service_connector")
    ]
    # Per run: 1 litellm + 1 connector deploy (no second-pass redeploy
    # under the shortened-name URL-determinism path). Two runs => 2 + 2.
    assert len([c for c in deploy_calls if c[0] == "deploy_litellm_proxy"]) == 2
    assert len([c for c in deploy_calls if c[0] == "deploy_remote_service_connector"]) == 2


def test_deploy_env_dev_neon_failure_does_not_inline_rollback(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """Modal env created; Neon failed -> the exception propagates, no inline rollback.

    Inline best-effort rollback is gone; ``minds env recover`` (added in
    a later phase) is the canonical recovery path. For now, partial
    state is left in place and the exception surfaces to the operator.
    """
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log, fail_step="neon_project")
    with pytest.raises(NeonProviderError, match="neon create boom"):
        deploy_env(
            DevEnvName("dev-carol"),
            tier="dev",
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )
    step_names = [c[0] for c in call_log["calls"]]
    # No delete_* calls fire on failure -- recover is the rollback path.
    assert step_names == ["ensure_modal_env", "create_neon_project"]
    # No client.toml / secrets.toml written for a failed deploy.
    assert not client_config_exists(DevEnvName("dev-carol"))


def test_deploy_env_dev_supertokens_failure_does_not_inline_rollback(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log, fail_step="supertokens_app")
    with pytest.raises(SuperTokensProviderError, match="supertokens create boom"):
        deploy_env(
            DevEnvName("dev-dan"),
            tier="dev",
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )
    step_names = [c[0] for c in call_log["calls"]]
    # No delete_* calls fire -- recover handles rollback in a later phase.
    assert step_names == [
        "ensure_modal_env",
        "create_neon_project",
        "create_supertokens_app",
    ]


def test_deploy_dev_env_pushes_per_env_secrets_into_dev_modal_env(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """On a clean deploy, every per-env push targets the dev env's Modal env."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("dev-frank"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    pushes = [c for c in call_log["calls"] if c[0] == "push_per_env_modal_secret"]
    # _deploy_config declares only `cloudflare` in its services list, so
    # one Vault-backed Modal Secret gets pushed. The deploy also pushes
    # the ``litellm-connector`` Modal Secret separately (its values
    # are 100% deploy-time-computed; not vault-backed; see the
    # ``[secrets].services`` comment in tier deploy.tomls). Names are
    # timestamped ``<prefix>-<deploy_id>`` -- check the prefix since
    # the id varies per run.
    pushed_secret_prefixes = {c[1].rsplit("-", 1)[0] for c in pushes}
    assert pushed_secret_prefixes == {"cloudflare-dev", "litellm-connector-dev"}
    # All pushes target the same Modal env (the dev env name 'dev-frank') --
    # not the tier's stable 'main' env. Two devs never share one Modal env.
    assert all(c[2] == "dev-frank" for c in pushes)
    deploys = [c for c in call_log["calls"] if c[0].startswith("deploy_") and c[0] != "deploy_mngr_agent"]
    # Single connector deploy (no second-pass redeploy): the shortened
    # app + function names keep the Modal hostname under DNS's 63-char
    # limit so the URL we computed up front equals the URL Modal
    # assigns, and the URL-dependent secrets are correct on first push.
    # Strip the deploy_id (last tuple element, varies per run) before
    # asserting the call shape.
    assert [c[:4] for c in deploys] == [
        ("deploy_litellm_proxy", "dev-frank", "dev", 0),
        ("deploy_remote_service_connector", "dev-frank", "dev", 0),
    ]


def test_destroy_env_dev_walks_providers_in_order_and_removes_root(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """Dev destroy: mngr agents first, then cloud resources, env root LAST."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("dev-george"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    assert env_root_exists(DevEnvName("dev-george"))
    call_log["calls"].clear()

    destroy_env(
        DevEnvName("dev-george"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    step_names = [c[0] for c in call_log["calls"]]
    assert step_names == [
        # Step 1: mngr agents are listed but none exist in the fresh
        # env root; no destroy_mngr_agents call.
        # Step 1b: state-container cleanup still runs (independent of agents).
        "cleanup_state_container",
        # Step 2: OVH.
        "list_ovh_instances",
        # Step 3: read CF Vault entry + enumerate this env's tunnels.
        "read_per_env_secret_values",
        "list_cloudflare_tunnels_for_env",
        # (No `delete_cloudflare_tunnels` -- fake returns empty list.)
        # Step 4: SuperTokens app (cascade-deletes its users).
        "delete_supertokens_app",
        # Step 5: Neon project (atomic teardown of all DBs / roles / endpoints).
        "delete_neon_project",
        # Step 6: Modal env (cascade-deletes apps/secrets/volumes inside).
        "delete_modal_env",
        # Step 7: env root removal happens after all provider calls succeed.
    ]
    # Env root removed so subsequent commands fail fast on a dangling
    # activation rather than silently re-creating partial state.
    assert not env_root_exists(DevEnvName("dev-george"))


def test_destroy_env_dev_destroys_mngr_agents_before_cloud_teardown(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """When the env root has mngr agents, destroy must clean them up FIRST."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("dev-kim"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    # Seed two fake agent dirs under the env's mngr profile.
    agents_dir = _isolated_home / ".minds-dev-kim" / "mngr" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "agent-1111").mkdir()
    (agents_dir / "agent-2222").mkdir()
    call_log["calls"].clear()

    destroy_env(
        DevEnvName("dev-kim"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    # A single destroy_mngr_agents call (all ids at once) then the
    # state-container cleanup, BEFORE any cloud-side teardown.
    step_names = [c[0] for c in call_log["calls"]]
    first_cloud_index = step_names.index("list_ovh_instances")
    assert step_names[:first_cloud_index] == ["destroy_mngr_agents", "cleanup_state_container"]
    agent_id_batches = [c[1] for c in call_log["calls"] if c[0] == "destroy_mngr_agents"]
    assert agent_id_batches == [("agent-1111", "agent-2222")]


def test_destroy_env_dev_keep_agents_skips_mngr_destroy(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """The legacy keep_agents=True flag must skip the mngr-agent step entirely."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("dev-liz"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    agents_dir = _isolated_home / ".minds-dev-liz" / "mngr" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "agent-1111").mkdir()
    call_log["calls"].clear()

    destroy_env(
        DevEnvName("dev-liz"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
        keep_agents=True,
    )
    step_names = [c[0] for c in call_log["calls"]]
    assert "destroy_mngr_agents" not in step_names
    # keep_agents must also skip the state-container cleanup: kept agents
    # still rely on the singleton state container.
    assert "cleanup_state_container" not in step_names


def test_destroy_env_dev_leaves_env_root_when_step_fails(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """If any cleanup step fails, the env root must stay so re-runs can recover."""
    # First, do a successful deploy so the env root exists.
    providers_ok = _build_fake_providers(_make_call_log())
    deploy_env(
        DevEnvName("dev-matt"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers_ok,
        parent_concurrency_group=_root_cg,
    )
    assert env_root_exists(DevEnvName("dev-matt"))

    # Now destroy with a provider that fails on neon_project delete -- env
    # root must NOT be removed.
    failing_providers = _build_fake_providers(_make_call_log(), fail_delete={"neon_project"})
    with pytest.raises(NeonProviderError, match="neon delete boom"):
        destroy_env(
            DevEnvName("dev-matt"),
            tier="dev",
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=failing_providers,
            parent_concurrency_group=_root_cg,
        )
    assert env_root_exists(DevEnvName("dev-matt"))


def test_destroy_deletes_ovh_instances(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    instances = (
        IamResource(
            urn="urn:v1:us:resource:vps:vps-a.vps.ovh.us",
            name="vps-a.vps.ovh.us",
            type="vps",
            tags={"minds_env": "hank"},
        ),
        IamResource(
            urn="urn:v1:us:resource:vps:vps-b.vps.ovh.us",
            name="vps-b.vps.ovh.us",
            type="vps",
            tags={"minds_env": "hank"},
        ),
    )
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log, ovh_instances=instances)
    deploy_env(
        DevEnvName("dev-hank"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    call_log["calls"].clear()

    destroy_env(
        DevEnvName("dev-hank"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    delete_call = next(c for c in call_log["calls"] if c[0] == "delete_ovh_instances")
    assert delete_call == ("delete_ovh_instances", 2)


def test_destroy_missing_env_proceeds_with_cloud_cleanup(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Destroy proceeds with cloud-side cleanup even when the local env root is gone.

    See F22 in MANUAL_DEPLOY_FINDINGS.md -- the previous behaviour was to
    raise DevEnvNotFoundError, which left orphaned cloud resources
    unreachable. The cloud resources are keyed off the env name, not
    the local directory, so destroy can converge without the directory.
    """
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    destroy_env(
        DevEnvName("dev-ghost"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    # Cloud-side steps still fire even though the local root is missing.
    step_names = [c[0] for c in call_log["calls"]]
    assert "delete_modal_env" in step_names
    assert "delete_neon_project" in step_names
    assert "delete_supertokens_app" in step_names


def test_list_dev_envs_returns_summaries_in_sorted_order(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    providers = _build_fake_providers(_make_call_log())
    deploy_env(
        DevEnvName("dev-ivy"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploy_env(
        DevEnvName("dev-juan"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    summaries = list_dev_envs()
    names = [s.name for s in summaries]
    assert names == ["dev-ivy", "dev-juan"]
    assert all(str(s.connector_url).startswith(f"https://{_TEST_MODAL_WORKSPACE}-") for s in summaries)


def test_list_dev_envs_treats_minds_dir_as_production_row(
    _isolated_home: Path,
) -> None:
    """Production lives at ~/.minds/ and shows up in list_dev_envs as 'production'.

    Per F11, production's client.toml is the committed in-repo file at
    ``apps/minds/imbue/minds/config/envs/production/client.toml`` -- the
    list helper falls back to that file for the reserved tier names.
    """
    (_isolated_home / ".minds").mkdir()
    summaries = list_dev_envs()
    assert any(s.name == "production" for s in summaries)
    prod = next(s for s in summaries if s.name == "production")
    assert prod.client_config_source == "in_repo"
    # The committed in-repo file ships with a parseable connector URL.
    assert prod.client_config_path is not None
    assert "config/envs/production/client.toml" in prod.client_config_path
    assert prod.connector_url is not None


def test_list_dev_envs_marks_dev_env_without_client_toml_as_no_client(
    _isolated_home: Path,
) -> None:
    """A freshly-mkdir'd ~/.minds-dev-josh-3/ (no client.toml yet) still shows up."""
    (_isolated_home / ".minds-dev-josh-3").mkdir()
    summaries = list_dev_envs()
    assert [s.name for s in summaries] == ["dev-josh-3"]
    only = summaries[0]
    assert only.connector_url is None
    assert only.client_config_path is None


# ---------- deploy_tier_env (staging / production path) ----------


def test_deploy_env_shared_tier_writes_nothing_to_disk(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Shared-tier deploy never touches the per-env on-disk state (``writes_local_state=false``)."""
    providers = _build_fake_providers(_make_call_log())
    result = deploy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    assert result.tier == "staging"
    assert result.modal_env == "main"
    assert result.client_config_path is None
    assert result.secrets_path is None
    # ~/.minds/, ~/.minds-staging/ are not created by shared-tier deploy.
    assert not (_isolated_home / ".minds-staging").exists()
    assert not (_isolated_home / ".minds").exists()


def test_deploy_env_shared_tier_pushes_secrets_into_named_modal_env(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """Shared-tier deploy pushes every declared service into the tier's stable Modal env."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("production"),
        tier="production",
        deploy_config=_deploy_config(tier="production", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    pushes = [c for c in call_log["calls"] if c[0] == "push_per_env_modal_secret"]
    # _deploy_config declares only `cloudflare` in its services list,
    # plus the deploy always pushes a separate ``litellm-connector``
    # Modal Secret (not vault-backed; values 100% deploy-time-computed).
    pushed_secret_prefixes = {c[1].rsplit("-", 1)[0] for c in pushes}
    assert pushed_secret_prefixes == {"cloudflare-production", "litellm-connector-production"}
    # All pushes target the tier's stable Modal env, not a per-dev one.
    assert all(c[2] == "main" for c in pushes)


def test_deploy_env_shared_tier_runs_both_modal_deploys(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """llm + rsc both get deployed -- in that order."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploys = [c for c in call_log["calls"] if c[0].startswith("deploy_") and c[0] != "deploy_mngr_agent"]
    assert [c[:4] for c in deploys] == [
        ("deploy_litellm_proxy", "main", "staging", 0),
        ("deploy_remote_service_connector", "main", "staging", 0),
    ]


def test_deploy_env_shared_tier_threads_min_containers_through(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """``[min_containers]`` from deploy.toml lands on each deploy call.

    Mirrors the committed shape of staging / production deploy.toml
    (``connector = 1, litellm_proxy = 1``) so a regression in the
    threading path surfaces here before a real deploy ever runs.
    """
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(
            tier="staging",
            min_containers=MinContainersConfig(connector=NonNegativeInt(2), litellm_proxy=NonNegativeInt(3)),
        ),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploys = [c for c in call_log["calls"] if c[0].startswith("deploy_") and c[0] != "deploy_mngr_agent"]
    assert [c[:4] for c in deploys] == [
        ("deploy_litellm_proxy", "main", "staging", 3),
        ("deploy_remote_service_connector", "main", "staging", 2),
    ]


def test_deploy_env_threads_scaledown_window_through(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """``[scaledown_window]`` from deploy.toml lands on each deploy call.

    Mirrors the committed shape of the dev tier (``connector = 600,
    litellm_proxy = 600``) so a regression in the threading path surfaces
    here before a real deploy. The scaledown window is logged at index 4
    of each deploy call tuple (right after min_containers).
    """
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(
            tier="staging",
            scaledown_window=ScaledownWindowConfig(connector=NonNegativeInt(600), litellm_proxy=NonNegativeInt(450)),
        ),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    deploys = [c for c in call_log["calls"] if c[0].startswith("deploy_") and c[0] != "deploy_mngr_agent"]
    # Tuple shape: (name, modal_env, tier, min_containers, scaledown_window, ...).
    assert [(c[0], c[4]) for c in deploys] == [
        ("deploy_litellm_proxy", 450),
        ("deploy_remote_service_connector", 600),
    ]


def test_deploy_env_seeds_default_paid_entries(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """``[paid]`` defaults from deploy.toml are seeded after migrations.

    Mirrors the committed shape (every tier defaults ``domains=["imbue.com"]``)
    so a regression in the seed-threading path surfaces here before a real
    deploy. The seed call must come after apply_pool_hosts_migrations.
    """
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("dev-josh"),
        tier="dev",
        deploy_config=_deploy_config(
            tier="dev",
            paid=PaidDefaultsConfig(domains=(NonEmptyStr("imbue.com"),)),
        ),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    seed_calls = [c for c in call_log["calls"] if c[0] == "seed_paid_list_defaults"]
    assert len(seed_calls) == 1
    # Tuple shape: (name, dsn, domains, emails).
    assert seed_calls[0][2] == ("imbue.com",)
    assert seed_calls[0][3] == ()
    # Seed runs after the migration step.
    assert _step_position(call_log, "seed_paid_list_defaults") > _step_position(
        call_log, "apply_pool_hosts_migrations"
    )


def test_deploy_env_skips_paid_seed_when_no_defaults(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """A tier with no ``[paid]`` defaults performs no seed call."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("dev-josh"),
        tier="dev",
        # PaidDefaultsConfig() -> empty
        deploy_config=_deploy_config(tier="dev"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    assert not [c for c in call_log["calls"] if c[0] == "seed_paid_list_defaults"]


def test_destroy_env_tier_stops_apps_deletes_secrets_and_removes_env_root(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """Tier destroy: agents -> modal app stop -> per-tier secret delete -> env root."""
    # Do a real deploy first so the fake "pushes" the timestamped Secrets
    # that destroy will then list + delete. This mirrors the real
    # operator workflow (you only destroy what you deployed).
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    _isolated_home.joinpath(".minds-staging").mkdir(exist_ok=True)
    deploy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    call_log["calls"].clear()
    destroy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )

    # Stops both apps in the tier's Modal env, in deploy order (litellm first).
    stops = [c for c in call_log["calls"] if c[0] == "stop_modal_app"]
    assert stops == [
        ("stop_modal_app", "llm-staging", "main"),
        ("stop_modal_app", "rsc-staging", "main"),
    ]
    # Deletes every timestamped per-tier Modal Secret. The _deploy_config
    # only lists `cloudflare` in [secrets].services, but the deploy also
    # pushes a separate `litellm-connector` Modal Secret (not vault-
    # backed; values 100% deploy-time-computed), so destroy's
    # ``gc_old_per_tier_secrets`` sweep deletes both.
    deletes = [c for c in call_log["calls"] if c[0] == "delete_modal_secret"]
    deleted_prefixes = {c[1].rsplit("-", 1)[0] for c in deletes}
    assert deleted_prefixes == {"cloudflare-staging", "litellm-connector-staging"}
    assert all(c[2] == "main" for c in deletes)
    # And stop comes BEFORE every delete (otherwise the running app
    # loses its secret out from under it).
    first_delete_idx = min(call_log["calls"].index(d) for d in deletes)
    assert call_log["calls"].index(stops[-1]) < first_delete_idx
    # Env root gone -- subsequent activation has to re-create it.
    assert not (_isolated_home / ".minds-staging").exists()


def test_destroy_env_tier_destroys_mngr_agents_first(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Tier destroy must `mngr destroy` any agents under the env root before cloud teardown."""
    # Seed an env root + a couple of fake agent dirs under it.
    staging_root = _isolated_home / ".minds-staging"
    agents_dir = staging_root / "mngr" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "agent-9999").mkdir()
    (agents_dir / "agent-8888").mkdir()

    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    destroy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    # A single destroy_mngr_agents call (sorted ids) BEFORE any stop_modal_app.
    agent_calls = [c for c in call_log["calls"] if c[0] == "destroy_mngr_agents"]
    assert [c[1] for c in agent_calls] == [("agent-8888", "agent-9999")]
    first_app_index = next(i for i, c in enumerate(call_log["calls"]) if c[0] == "stop_modal_app")
    last_agent_index = next(
        i for i, c in reversed(list(enumerate(call_log["calls"]))) if c[0] == "destroy_mngr_agents"
    )
    assert last_agent_index < first_app_index


def test_destroy_env_tier_proceeds_when_env_root_missing(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Unified destroy proceeds with cloud cleanup even without the env root.

    See F22 in MANUAL_DEPLOY_FINDINGS.md: the env root is a convenience
    pointer, not authoritative -- the cloud-side resources are keyed
    off the env name (Modal env, Modal apps, Neon, SuperTokens,
    Cloudflare tags, OVH tags), so destroy can converge purely by
    name. Refusing on missing-root would orphan cloud state for
    operators who manually nuke the directory.
    """
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    destroy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    # Shared-tier cleanup still fires (stop modal apps + secret GC).
    step_names = [c[0] for c in call_log["calls"]]
    assert "stop_modal_app" in step_names


def test_destroy_env_tier_leaves_env_root_when_step_fails(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """If any cleanup step fails, the env root must stay so re-runs can recover."""
    staging_root = _isolated_home / ".minds-staging"
    staging_root.mkdir()

    failing_providers = _build_fake_providers(_make_call_log(), fail_step="stop_modal_app")
    with pytest.raises(ModalDeployError, match="modal app stop boom"):
        destroy_env(
            DevEnvName("staging"),
            tier="staging",
            deploy_config=_deploy_config(tier="staging", modal_env="main"),
            credentials=_credentials(),
            providers=failing_providers,
            parent_concurrency_group=_root_cg,
        )
    assert staging_root.exists()


def test_destroy_env_tier_wipes_supertokens_app_with_parsed_app_id(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """SuperTokens wipe must extract the app_id from the Vault connection URI."""
    staging_root = _isolated_home / ".minds-staging"
    staging_root.mkdir()
    call_log = _make_call_log()
    providers = _build_fake_providers(
        call_log,
        vault_responses={
            "supertokens": {
                "SUPERTOKENS_CONNECTION_URI": "https://st.imbue.com/appid-my-staging-app",
                "SUPERTOKENS_API_KEY": "secret-key-xyz",
            },
            "neon": {"DATABASE_URL": "postgres://x"},
            "cloudflare": {"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t"},
        },
    )
    destroy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    st_calls = [c for c in call_log["calls"] if c[0] == "wipe_supertokens_app_data"]
    assert st_calls == [("wipe_supertokens_app_data", "my-staging-app", "https://st.imbue.com")]


def test_destroy_env_tier_wipes_neon_with_dsn_from_vault(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """Neon wipe must use the DATABASE_URL from the tier Vault entry."""
    staging_root = _isolated_home / ".minds-staging"
    staging_root.mkdir()
    call_log = _make_call_log()
    providers = _build_fake_providers(
        call_log,
        vault_responses={
            "supertokens": {
                "SUPERTOKENS_CONNECTION_URI": "https://st/appid-staging",
                "SUPERTOKENS_API_KEY": "k",
            },
            "neon": {"DATABASE_URL": "postgres://realuser:realpass@neon.host/realdb"},
            "cloudflare": {"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t"},
        },
    )
    destroy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    neon_calls = [c for c in call_log["calls"] if c[0] == "wipe_neon_db_schema"]
    assert neon_calls == [("wipe_neon_db_schema", "postgres://realuser:realpass@neon.host/realdb")]


def test_destroy_env_tier_refuses_when_supertokens_vault_entry_incomplete(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """A misconfigured / missing Vault entry must fail loud, not skip the wipe."""
    staging_root = _isolated_home / ".minds-staging"
    staging_root.mkdir()
    providers = _build_fake_providers(
        _make_call_log(),
        vault_responses={
            "supertokens": {},
            "neon": {"DATABASE_URL": "postgres://x"},
            "cloudflare": {"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t"},
        },
    )
    with pytest.raises(MindError, match="SUPERTOKENS_CONNECTION_URI"):
        destroy_env(
            DevEnvName("staging"),
            tier="staging",
            deploy_config=_deploy_config(tier="staging", modal_env="main"),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )
    # Env root stays because the wipe step failed mid-flight.
    assert staging_root.exists()


def test_destroy_env_tier_full_step_order(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """End-to-end tier destroy: same step ordering as dev destroy (only resource-management ops differ + the generation-id removal is tier-only)."""
    staging_root = _isolated_home / ".minds-staging"
    agents_dir = staging_root / "mngr" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "agent-aaaa").mkdir()

    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    # Deploy first so destroy has timestamped Secrets to find + delete.
    deploy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    call_log["calls"].clear()
    destroy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging", modal_env="main"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    step_names = [c[0] for c in call_log["calls"]]
    assert step_names == [
        # 1: agents
        "destroy_mngr_agents",
        # 1b: state-container cleanup (independent of agents).
        "cleanup_state_container",
        # 2: OVH (shared with dev, by env name).
        "list_ovh_instances",
        # 3: CF tunnels (shared with dev, by env name).
        "read_per_env_secret_values",
        "list_cloudflare_tunnels_for_env",
        # 4: SuperTokens -- wipe path (tier-specific).
        "read_per_env_secret_values",
        "wipe_supertokens_app_data",
        # 5: Neon -- wipe path (tier-specific).
        "read_per_env_secret_values",
        "wipe_neon_db_schema",
        # 6: Modal -- stop + list-then-delete-all-timestamped-secrets path.
        # Two deletes: ``cloudflare-staging-<id>`` (the one entry in this
        # _deploy_config's [secrets].services) and ``litellm-connector-
        # staging-<id>`` (always pushed separately by the deploy as a
        # non-vault-backed Modal Secret).
        "stop_modal_app",
        "stop_modal_app",
        "list_modal_secrets",
        "delete_modal_secret",
        "delete_modal_secret",
        # 7: generation id (tier-only).
        "delete_generation_id",
    ]
    # And env root is gone after the full flow succeeds.
    assert not staging_root.exists()


# -- F1 / F2 / F4: deploy-safety ordering invariants --------------------------
#
# Each test below pins one of the safety invariants we just (re)established
# in ``_deploy_env_locked``. Bare position assertions (rather than full
# step-order snapshots) so the tests don't break on every unrelated reorder.


def _step_position(call_log: dict[str, list], step_name: str) -> int:
    """Index of the first call to ``step_name`` in ``call_log``.

    Raises if the step never fired -- the absence is itself a useful test
    failure (vs. silently returning ``-1`` and producing a confusing
    "expected -1 < 0" message).
    """
    for idx, call in enumerate(call_log["calls"]):
        if call[0] == step_name:
            return idx
    raise AssertionError(f"step {step_name!r} never fired; calls were: {[c[0] for c in call_log['calls']]}")


def test_f1_snapshot_created_before_migrations_run(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """F1 invariant: snapshot + recover-target file write happen BEFORE migrations.

    Pre-fix, migrations ran first, then snapshot. A failed migration left
    no recover-target on disk + the snapshot captured the post-migration
    state, so recover could never undo a bad migration. Post-fix, the
    snapshot captures the pre-migration state and the recover-target file
    is on disk before the migration runs, so a failed migration is
    rolled back by ``minds env recover`` along with everything else.
    """
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("dev-f1-snapshot-before-migration"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    snapshot_pos = _step_position(call_log, "create_neon_snapshot_branch")
    migration_pos = _step_position(call_log, "apply_pool_hosts_migrations")
    assert snapshot_pos < migration_pos, (
        f"snapshot must run before migrations (snapshot at {snapshot_pos}, migrations at {migration_pos})"
    )


def test_f1_snapshot_created_before_migrations_run_shared_tier(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """F1 holds for shared tiers too -- and matters more there (live-traffic DB)."""
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("staging"),
        tier="staging",
        deploy_config=_deploy_config(tier="staging"),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    snapshot_pos = _step_position(call_log, "create_neon_snapshot_branch")
    migration_pos = _step_position(call_log, "apply_pool_hosts_migrations")
    assert snapshot_pos < migration_pos


def test_f2_verify_neon_token_scope_runs_before_snapshot(_isolated_home: Path, _root_cg: ConcurrencyGroup) -> None:
    """F2 invariant: the Neon token's read-scope is verified BEFORE the snapshot.

    Pre-fix, ``verify_neon_token_has_restore_scope`` was declared on the
    Providers bundle and wired to the real implementation but never
    called from the deploy path. A token without read access only failed
    when ``minds env recover`` actually tried to restore -- after the
    deploy had already mutated other state.
    """
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    deploy_env(
        DevEnvName("dev-f2-verify-scope"),
        tier="dev",
        deploy_config=_deploy_config(),
        credentials=_credentials(),
        providers=providers,
        parent_concurrency_group=_root_cg,
    )
    verify_pos = _step_position(call_log, "verify_neon_token_has_restore_scope")
    snapshot_pos = _step_position(call_log, "create_neon_snapshot_branch")
    assert verify_pos < snapshot_pos, (
        f"token-scope verify must run before snapshot (verify at {verify_pos}, snapshot at {snapshot_pos})"
    )


def test_f2_verify_neon_token_scope_failure_aborts_before_snapshot(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """A Neon-scope failure raises BEFORE the snapshot branch is created.

    Confirms the preflight wiring actually short-circuits the deploy
    (vs. the broken state where the verify wasn't called at all).
    """
    call_log = _make_call_log()
    base_providers = _build_fake_providers(call_log)

    # Replace verify_neon_token_has_restore_scope with one that raises.
    # Type-safe model_copy_update + field_ref + to_update per the style
    # guide's "Type-safe model_copy_update" section (so field renames
    # break the test, not the runtime).
    def failing_verify(project_id, api_token):
        call_log["calls"].append(("verify_neon_token_has_restore_scope", project_id))
        raise NeonProviderError("neon scope boom")

    providers = base_providers.model_copy_update(
        to_update(base_providers.field_ref().verify_neon_token_has_restore_scope, failing_verify),
    )

    with pytest.raises(NeonProviderError, match="neon scope boom"):
        deploy_env(
            DevEnvName("dev-f2-scope-failure"),
            tier="dev",
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )
    step_names = [c[0] for c in call_log["calls"]]
    assert "verify_neon_token_has_restore_scope" in step_names
    assert "create_neon_snapshot_branch" not in step_names, (
        f"snapshot must not run if scope verify failed; call sequence was: {step_names}"
    )
    assert "apply_pool_hosts_migrations" not in step_names, (
        f"migrations must not run if scope verify failed; call sequence was: {step_names}"
    )


def test_f4_snapshot_branch_deleted_when_recover_target_write_fails(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """F4 invariant: a failed recover-target file write deletes the just-created snapshot branch.

    Pre-fix, if ``write_recover_target_atomic`` raised after the
    snapshot branch was created in Neon, the branch was orphaned --
    no file pointed at it, so the operator had no
    ``minds env recover`` path to clean it up. Post-fix, the snapshot
    branch is deleted before the exception re-raises.

    Triggers the failure naturally (no monkeypatch needed) by
    pre-creating a DIRECTORY at the recover-target path. The early
    ``recover_target_exists`` check uses ``is_file()`` so a directory
    bypasses it; ``write_recover_target_atomic``'s own
    ``if final_path.exists()`` then raises
    :class:`RecoverTargetAlreadyExistsError` (a MindError +
    FileExistsError, which our try/except catches).
    """
    call_log = _make_call_log()
    providers = _build_fake_providers(call_log)
    env_name = "dev-f4-write-failure"

    # Pre-create a directory at exactly the recover-target path. The
    # early ``recover_target_exists`` check uses ``is_file()`` (returns
    # False for a dir), so the deploy proceeds through provider creation
    # + snapshot creation; only when ``write_recover_target_atomic``'s
    # ``final_path.exists()`` check fires does it raise. Builds the path
    # the same way the production code does so this test breaks if the
    # naming convention changes.
    recover_target_dir = _isolated_home / f".minds-deploy-recover-target-{env_name}.json"
    recover_target_dir.mkdir()

    with pytest.raises(RecoverTargetAlreadyExistsError):
        deploy_env(
            DevEnvName(env_name),
            tier="dev",
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )

    step_names = [c[0] for c in call_log["calls"]]
    snapshot_pos = step_names.index("create_neon_snapshot_branch")
    # The snapshot WAS created (we got past that step before the write failed)...
    assert "create_neon_snapshot_branch" in step_names
    # ...and then deleted by the F4 best-effort cleanup before the exception re-raised.
    assert "delete_neon_branch" in step_names[snapshot_pos:], (
        "delete_neon_branch must fire after the failed write to clean up the just-created snapshot; "
        f"call sequence after snapshot was: {step_names[snapshot_pos:]}"
    )
    # Sanity: no later deploy step ran (the write failure aborted the deploy).
    assert "apply_pool_hosts_migrations" not in step_names, (
        f"migrations must not run after the recover-target write failed; calls: {step_names}"
    )
    assert "push_per_env_modal_secret" not in step_names


def test_f4_recover_target_write_failure_logs_but_propagates_when_cleanup_also_fails(
    _isolated_home: Path, _root_cg: ConcurrencyGroup
) -> None:
    """If snapshot-branch cleanup itself fails, the original write error still propagates.

    The compounded failure is logged as a warning (so the operator
    knows the snapshot is orphaned), but the user-visible exception is
    still the RecoverTargetAlreadyExistsError from the write -- the
    original cause, not the cleanup secondary.
    """
    call_log = _make_call_log()
    base_providers = _build_fake_providers(call_log)
    env_name = "dev-f4-cleanup-also-fails"

    # Pre-create a directory at the recover-target path (same trick as
    # the previous test) so write_recover_target_atomic raises naturally.
    recover_target_dir = _isolated_home / f".minds-deploy-recover-target-{env_name}.json"
    recover_target_dir.mkdir()

    def failing_delete(project_id, branch_id, api_token):
        call_log["calls"].append(("delete_neon_branch", project_id, branch_id))
        raise NeonProviderError("neon delete boom")

    providers = base_providers.model_copy_update(
        to_update(base_providers.field_ref().delete_neon_branch, failing_delete),
    )

    # Original RecoverTargetAlreadyExistsError (not the secondary
    # NeonProviderError) is what propagates -- the user needs the root
    # cause, not the cleanup noise.
    with pytest.raises(RecoverTargetAlreadyExistsError):
        deploy_env(
            DevEnvName(env_name),
            tier="dev",
            deploy_config=_deploy_config(),
            credentials=_credentials(),
            providers=providers,
            parent_concurrency_group=_root_cg,
        )

    # The cleanup attempt fired even though it ultimately failed.
    step_names = [c[0] for c in call_log["calls"]]
    assert "delete_neon_branch" in step_names
