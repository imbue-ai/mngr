"""``minds_deployment`` test: full create / destroy round-trip of a dev env.

Asserts that ``minds env deploy`` from clean creates every expected
cloud-side resource and that ``minds env destroy`` removes every one of
them. The shared-env stand-up the orchestrator already does is itself a
"deploy works" smoke; this test pairs it with an explicit destroy
assertion that the shared envs never exercise.

Currently skipped -- iterates with the rest of the suite once the
``ephemeral_env`` fixture's ``minds env deploy`` shell-out lands.
"""

import pytest

from imbue.minds.deployment_tests.data_types import EphemeralEnvHandle

pytestmark = pytest.mark.minds_deployment


@pytest.mark.skip(
    reason=(
        "Pending implementation of the ephemeral_env fixture's `minds env deploy` shell-out "
        "and the per-provider 'is this resource really gone' assertions. See "
        "specs/minds-deployment-tests.md > Initial test inventory."
    )
)
def test_deploy_then_destroy_round_trip(ephemeral_env: EphemeralEnvHandle) -> None:
    """Deploy from clean, assert every resource exists, destroy, assert every resource is gone.

    Planned assertions (post-deploy):
    - Modal env for ``ephemeral_env.name`` exists.
    - Both Modal apps (``rsc-dev`` + ``llm-dev``) are deployed and their
      ``/healthcheck`` endpoints return 200.
    - Neon project named ``minds-<env>`` exists with ``host_pool`` +
      ``litellm_cost`` databases inside.
    - SuperTokens app for ``<env>`` exists.
    - ``~/.minds-<env>/client.toml`` + ``secrets.toml`` exist with the
      right shape and mode 0600 for the secrets file.
    - Vault contains a generation id for the env (when the tier tracks
      generations -- dev does not today, so this is a no-op assertion
      for dev envs).

    Planned assertions (post-destroy, after calling ``minds env destroy``):
    - All of the above are gone.
    - No OVH / Cloudflare resources remain tagged with the env name.
    - ``~/.minds-<env>/`` is removed.
    - The ``ephemeral_env`` fixture's teardown is a no-op for an
      already-destroyed env (uses the same env-root presence check
      ``minds env destroy`` itself relies on).
    """
    raise AssertionError("not implemented yet -- see skip reason")
