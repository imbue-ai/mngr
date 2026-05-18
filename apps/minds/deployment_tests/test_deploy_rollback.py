"""``minds_deployment`` test: auto-rollback on a broken connector ``/healthcheck``.

Drives the v1 -> broken-v2 sequence and asserts the existing
``minds env deploy`` auto-rollback path restores v1 when
``await_apps_healthy`` fails. Does *not* deploy a clean v3 -- the
"redeploy advances version" contract is covered separately by
``test_deploy_new_version``.

Note: as of this PR the connector reads ``MINDS_INJECT_BROKEN_HEALTHCHECK=1``
per-request and returns 500 when set. The auto-rollback wiring in
``minds env deploy`` (calling ``rollback_modal_app`` when
``await_apps_healthy`` raises ``HealthCheckFailedError``) does NOT
exist today -- ``provisioning.py`` lets the exception bubble. Adding
that wiring is the production-code change this test will exercise once
it goes live.

Currently skipped -- iterates with the rest of the suite once both the
auto-rollback wiring and the ``ephemeral_env`` fixture are implemented.
"""

import pytest

from imbue.minds.deployment_tests.data_types import EphemeralEnvHandle

pytestmark = pytest.mark.minds_deployment


@pytest.mark.skip(
    reason=(
        "Pending the auto-rollback wiring in minds env deploy (call rollback_modal_app on "
        "await_apps_healthy failure) and the ephemeral_env fixture implementation. See "
        "specs/minds-deployment-tests.md."
    )
)
def test_deploy_auto_rollback_on_broken_healthcheck(ephemeral_env: EphemeralEnvHandle) -> None:
    """v2 deploys with a broken /healthcheck; assert auto-rollback restores v1.

    Planned flow:
    1. Capture v1 Modal app version id for the connector (via
       ``modal app describe`` shellout or the equivalent provider call).
    2. Deploy v2 with ``MINDS_INJECT_BROKEN_HEALTHCHECK=1`` threaded
       into the connector's deploy-secret bundle.
    3. Assert: ``minds env deploy`` exits non-zero, the live connector
       Modal app version is back at v1's id, and ``/healthcheck``
       returns 200 (proving the rollback's v1 is the version actually
       serving traffic, not just a label).

    Intentionally does not deploy a v3 -- ``test_deploy_new_version``
    covers the "redeploy advances version" contract.
    """
    raise AssertionError("not implemented yet -- see skip reason")
