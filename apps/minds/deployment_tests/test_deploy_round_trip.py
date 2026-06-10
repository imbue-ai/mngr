"""``minds_deployment`` test: full create / destroy round-trip of a CI ephemeral env.

Asserts that ``minds env deploy`` from clean creates every expected
cloud-side resource and that ``minds env destroy`` removes every one of
them. The shared-env stand-up the orchestrator already does is itself a
"deploy works" smoke; this test pairs it with an explicit destroy
assertion that the shared envs never exercise.

The ephemeral_env fixture has already done the deploy and given us a
handle; this body asserts the post-deploy state, then runs ``minds env
destroy`` explicitly and asserts the post-destroy state. The fixture's
teardown ``destroy`` then no-ops (env root already gone).
"""

import subprocess
from pathlib import Path

import httpx
import pytest
from loguru import logger
from pydantic import SecretStr

from imbue.minds.deployment_tests.data_types import EphemeralEnvHandle
from imbue.minds.deployment_tests.helpers import build_minds_env_subprocess_env
from imbue.minds.deployment_tests.helpers import load_ci_credentials_from_vault
from imbue.minds.deployment_tests.helpers import modal_env_exists
from imbue.minds.deployment_tests.helpers import neon_project_exists
from imbue.minds.deployment_tests.helpers import supertokens_app_exists
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.paths import secrets_file

_REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.minds_deployment

# The fixture's deploy (~3 min) + this test's destroy (~2 min) + the
# four cloud probes (a few seconds each) fit well under this.
_TEST_TIMEOUT_SECONDS = 15 * 60

_DESTROY_TIMEOUT_SECONDS = 10 * 60
_REQUEST_TIMEOUT_SECONDS = 30.0


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_deploy_then_destroy_round_trip(ephemeral_env: EphemeralEnvHandle) -> None:
    """Deploy creates every resource; destroy removes them all.

    Post-deploy assertions:
    - Modal env named ``<ephemeral_env.name>`` exists in the ci-tier workspace.
    - Connector + litellm-proxy /health/liveness both 200 (proxied via
      the just-deployed Modal apps).
    - Neon project ``minds-<name>`` exists under the ci-tier Neon org.
    - SuperTokens app ``<name>`` exists in the ci-tier core.
    - ``~/.minds-<name>/client.toml`` + ``secrets.toml`` exist on disk.

    Post-destroy assertions: each of the above is gone.

    The ci tier doesn't currently use OVH or Cloudflare resources for
    an ephemeral env (those come in once workspaces / tunnels exist),
    so those provider enumerations aren't asserted here.
    """
    creds = load_ci_credentials_from_vault()
    neon_org_id = creds["NEON_ORG_ID"]
    neon_api_token = SecretStr(creds["NEON_API_TOKEN"])
    supertokens_core_url = creds["SUPERTOKENS_CONNECTION_URI"]
    supertokens_api_key = SecretStr(creds["SUPERTOKENS_API_KEY"])

    # === Post-deploy state ===
    # Modal env created.
    assert modal_env_exists(ephemeral_env.name), (
        f"Modal env {ephemeral_env.name!r} not found in `modal environment list` after deploy."
    )

    # Both Modal apps reachable.
    connector_url = str(ephemeral_env.connector_url).rstrip("/")
    litellm_url = str(ephemeral_env.litellm_proxy_url).rstrip("/")
    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        connector_health = client.get(f"{connector_url}/health/liveness")
        litellm_health = client.get(f"{litellm_url}/health/liveness")
    assert connector_health.status_code == 200, (connector_health.status_code, connector_health.text[:300])
    assert litellm_health.status_code == 200, (litellm_health.status_code, litellm_health.text[:300])

    # Neon project for this env exists.
    assert neon_project_exists(name=ephemeral_env.name, org_id=neon_org_id, api_token=neon_api_token), (
        f"Neon project `minds-{ephemeral_env.name}` not found under org {neon_org_id!r} after deploy."
    )

    # SuperTokens app for this env exists.
    assert supertokens_app_exists(
        name=ephemeral_env.name, core_base_url=supertokens_core_url, api_key=supertokens_api_key
    ), f"SuperTokens app {ephemeral_env.name!r} not present in core after deploy."

    # Local state files written.
    env_root = env_root_dir(ephemeral_env.name)
    assert env_root.is_dir(), env_root
    client_toml = client_config_file(ephemeral_env.name)
    secrets_toml = secrets_file(ephemeral_env.name)
    assert client_toml.is_file(), client_toml
    assert secrets_toml.is_file(), secrets_toml

    # === Destroy ===
    _run_minds_env_destroy(ephemeral_env.name)

    # === Post-destroy state ===
    assert not modal_env_exists(ephemeral_env.name), (
        f"Modal env {ephemeral_env.name!r} still in `modal environment list` after destroy."
    )
    assert not neon_project_exists(name=ephemeral_env.name, org_id=neon_org_id, api_token=neon_api_token), (
        f"Neon project `minds-{ephemeral_env.name}` still present after destroy."
    )
    assert not supertokens_app_exists(
        name=ephemeral_env.name, core_base_url=supertokens_core_url, api_key=supertokens_api_key
    ), f"SuperTokens app {ephemeral_env.name!r} still present in core after destroy."
    assert not env_root.exists(), f"Env root {env_root} still on disk after destroy."

    # ephemeral_env fixture teardown's destroy will no-op (env root gone).


def _run_minds_env_destroy(name) -> None:
    """Shell out to ``uv run minds env destroy`` for ``name`` and assert success."""
    sub_env = build_minds_env_subprocess_env(name)
    completed = subprocess.run(
        ["uv", "run", "minds", "env", "destroy"],
        env=sub_env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_DESTROY_TIMEOUT_SECONDS,
        check=False,
    )
    logger.info("=== destroy stdout ({}) ===\n{}", name, completed.stdout)
    logger.info("=== destroy stderr ({}) ===\n{}", name, completed.stderr)
    assert completed.returncode == 0, (
        f"`minds env destroy` for {name!r} exited {completed.returncode}. Stderr tail:\n{completed.stderr[-2000:]}"
    )
