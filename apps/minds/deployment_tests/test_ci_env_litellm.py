"""End-to-end check that the per-run CI env can authenticate + serve LLM traffic.

The single ``minds_services`` test the Phase 1 pipeline exists to prove out:
log in to the per-run CI env as the fixed CI test user (created at env-build
time), mint a LiteLLM key through the connector (which exercises the paid-account
gate), and make one real LLM call through the returned proxy ``base_url``.
"""

from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.mngr.utils.testing import get_short_random_string

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

# Cheapest model in the litellm proxy config; the call only needs to prove the
# key + proxy + upstream Anthropic creds all work, not exercise a big model.
_LLM_MODEL = "claude-haiku-4-5"
_HTTP_TIMEOUT_SECONDS = 60.0


@pytest.mark.timeout(180)
def test_login_mint_litellm_key_and_call_llm(
    shared_env: Callable[[str], SharedEnvHandle],
    ci_test_user: tuple[NonEmptyStr, SecretStr],
) -> None:
    env = shared_env("default")
    wait_for_env_ready(env)
    email, password = ci_test_user
    connector_url = str(env.urls.connector_url).rstrip("/")

    # Log in as the fixed CI user (created against this env's SuperTokens app at
    # env-build time) and mint a LiteLLM key through the connector.
    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        signin = client.post(
            f"{connector_url}/auth/signin",
            json={"email": str(email), "password": password.get_secret_value()},
        )
        signin.raise_for_status()
        signin_json = signin.json()
        assert signin_json.get("status") == "OK", f"connector /auth/signin returned non-OK: {signin_json!r}"
        access_token = signin_json["tokens"]["access_token"]

        key_response = client.post(
            f"{connector_url}/keys/create",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"key_alias": f"ci-smoke-{get_short_random_string()}", "max_budget": 1.0, "budget_duration": "30d"},
        )
        assert key_response.status_code == 200, (
            f"connector /keys/create failed ({key_response.status_code}); the paid-account gate or "
            f"litellm wiring is broken: {key_response.text[:400]!r}"
        )
        key_material = key_response.json()
        minted_key = key_material["key"]
        base_url = str(key_material["base_url"]).rstrip("/")

    # Make one real LLM call through the minted key + returned proxy base_url.
    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        completion = client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {minted_key}", "Content-Type": "application/json"},
            json={
                "model": _LLM_MODEL,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
            },
        )
    assert completion.status_code == 200, (
        f"LLM call through the minted key failed ({completion.status_code}): {completion.text[:400]!r}"
    )
    content = completion.json()["choices"][0]["message"]["content"]
    assert content.strip(), f"LLM returned an empty completion: {completion.json()!r}"
