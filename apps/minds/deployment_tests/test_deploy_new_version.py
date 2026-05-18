"""``minds_deployment`` test: re-deploying advances the live Modal app version.

The cheapest possible "redeploy actually deploys" assertion: deploy a
fresh env, capture the connector's Modal app version id, deploy again
(no functional changes -- ``minds env deploy`` mints a new
``MINDS_DEPLOY_ID`` every time anyway, which is enough to land a new
version), and assert the live version id moved forward.

Currently skipped -- iterates with the rest of the suite once the
``ephemeral_env`` fixture and the connector's ``/version`` endpoint are
both in place.
"""

import pytest

from imbue.minds.deployment_tests.data_types import EphemeralEnvHandle

pytestmark = pytest.mark.minds_deployment


@pytest.mark.skip(
    reason=(
        "Pending the connector's /version endpoint shipping to the deployed env and the "
        "ephemeral_env fixture's `minds env deploy` shell-out. See specs/minds-deployment-tests.md."
    )
)
def test_deploy_new_version(ephemeral_env: EphemeralEnvHandle) -> None:
    """Deploy twice; assert the connector's live ``/version`` reports a new ``deploy_id``.

    Planned flow:
    1. The ``ephemeral_env`` fixture has already done the v1 deploy and
       yielded a handle. Hit ``GET <connector_url>/version``; record
       ``v1_deploy_id``.
    2. Trigger a second ``minds env deploy`` against the same env (no
       arg changes -- the orchestration mints a fresh
       ``MINDS_DEPLOY_ID`` automatically).
    3. Hit ``GET <connector_url>/version`` again; record
       ``v2_deploy_id``.
    4. Assert ``v2_deploy_id != v1_deploy_id`` and that ``v2_deploy_id``
       sorts strictly greater than ``v1_deploy_id`` (the id format is
       lex-sortable per ``secret_lifecycle.DeployId``).
    """
    raise AssertionError("not implemented yet -- see skip reason")
