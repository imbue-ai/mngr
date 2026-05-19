"""``minds_services`` test: real-LLM-call-through-litellm via a local Docker FCT workspace.

The "but does this actually work" test for imbue_cloud LLM key minting
+ litellm proxy routing + Neon spend tracking. Drives a real local
Docker container running the FCT template with the ``imbue_cloud``
AI-key option, sends a real chat message via ``mngr message``, asserts
the message was processed AND that the spend landed in Neon.

Currently skipped -- iterates once the in-process desktop-client
workspace-creation driver is wired up and the ``verified_user``
fixture lands.
"""

from collections.abc import Callable

import pytest

from imbue.minds.deployment_tests.data_types import FctTemplateRef
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready

pytestmark = pytest.mark.minds_services


@pytest.mark.skip(
    reason=(
        "Pending: desktop-client workspace-create driver, Neon DSN query helper, "
        "verified_user fixture provisioning. Also requires a Docker daemon (operator-side "
        "today; Docker-in-Docker in the future offload services config). See "
        "specs/minds-deployment-tests.md."
    )
)
def test_litellm_spend_tracking_via_local_workspace(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
    fct_template_ref: FctTemplateRef,
) -> None:
    """Drive a real local FCT workspace + assert spend lands in Neon ``litellm_cost``.

    Defensive preamble (do this before any other step in every test
    body in this suite): wait for the env to be reachable so cold-boot
    / stale-container windows don't surface as flakes. The session-
    autouse fixture has already swept stale ``test-*@example.test``
    users by the time we reach this line.

    Planned flow:

    0. **Wait for env ready.** ``wait_for_env_ready(shared_env("default"))``.
    1. Drive the in-process desktop client (same shape as
       ``test_realistic_signup_verify_signin_create_tunnel_signout``)
       to create a workspace from ``fct_template_ref.as_mngr_template_arg()``
       configured with ``AIProvider.IMBUE_CLOUD`` so the agent's LLM
       calls flow through the shared env's ``litellm_proxy_url``.
    2. Wait for the workspace's chat agent to come up.
    3. Use ``mngr message`` (subprocess against the running container)
       to send a real chat message to claude inside.
    4. Assert claude responds in the container's transcript within a
       reasonable timeout (message actually got sent + processed).
    5. Query ``shared_env('default').neon_litellm_dsn`` for a row
       against this workspace's litellm key with non-zero spend, dated
       within the last few seconds.

    Runs locally against the operator's Docker daemon. When this moves
    to offload, the future ``offload-modal-minds-services.toml`` will
    enable Docker-in-Docker (mirroring ``offload-modal-acceptance.toml``).
    """
    wait_for_env_ready(shared_env("default"))
    _ = (verified_user, fct_template_ref)
    raise AssertionError("not implemented yet -- see skip reason")
