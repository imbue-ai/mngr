"""``minds_services`` test: realistic first-time-user end-to-end flow.

The only test in the suite that exercises the full real-email path
(signup -> verify -> sign-in-via-one-time-code) and pairs it with the
desktop client's workspace + system-interface-forwarding surface.
Combined intentionally per the spec: the verify + one-time-code flow
goes through mail.tm, so tacking the tunnel create/delete on the same
test reuses the same logged-in session and exercises the realistic
"first thing I do after signing up" flow.

Currently skipped -- iterates once the signup-API client, mail.tm
helpers, and in-process desktop-client driving are all wired up.
"""

from collections.abc import Callable

import pytest

from imbue.minds.deployment_tests._mailtm import MailtmInbox
from imbue.minds.deployment_tests.data_types import FctTemplateRef
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready

pytestmark = pytest.mark.minds_services


@pytest.mark.skip(
    reason=(
        "Pending: mail.tm-backed signup driver, one-time-code sign-in driver, in-process "
        "desktop client workspace + system-interface forwarding driver, Cloudflare-list "
        "assertion helper. See specs/minds-deployment-tests.md > Initial test inventory."
    )
)
def test_realistic_signup_verify_signin_create_tunnel_signout(
    shared_env: Callable[[str], SharedEnvHandle],
    signup_email: MailtmInbox,
    fct_template_ref: FctTemplateRef,
) -> None:
    """End-to-end first-time-user flow against the ``default`` shared env.

    Defensive preamble (do this before any other step in every test
    body in this suite): wait for the env to be reachable so cold-boot
    / stale-container windows don't surface as flakes. The session-
    autouse fixture has already swept stale ``test-*@example.test``
    users by the time we reach this line. The planned cleanup of any
    pre-existing Cloudflare tunnels owned by this fresh user is a
    no-op for a brand-new account but stays in the planned flow as
    defense-in-depth.

    Planned flow:

    0. **Wait for env ready.** ``wait_for_env_ready(shared_env("default"))``.
    1. **Signup.** POST to the connector's public sign-up endpoint with
       ``signup_email.address``.
    2. **Email verification.** ``signup_email.wait_for_verification_token()``
       polls mail.tm for the verification email; POST the token to the
       connector's ``/verify-email`` endpoint.
    3. **Sign-in (one-time code).** Trigger the connector's
       email-one-time-code sign-in flow (the only sign-in path today --
       no password); ``signup_email.wait_for_one_time_code()`` polls
       mail.tm again; submit the code to complete sign-in; assert
       session cookie / token returned.
    4. **Workspace creation.** Use the in-process desktop client (same
       ``create_desktop_client(...)`` pattern as the existing
       ``test_desktop_client_e2e.py``) to create a workspace from
       ``fct_template_ref.as_mngr_template_arg()``. Wait for the
       workspace to reach a running state.
    5. **Forward the system-interface.** Drive the desktop client's
       "forward system-interface" action for the workspace -- this is
       the user-facing operation that creates the Cloudflare tunnel
       pointing at the workspace's system-interface port. Assert the
       tunnel exists in Cloudflare (list, filtered by env tag) AND the
       forwarded URL serves the expected response when hit from the
       test process.
    6. **Teardown.** Stop forwarding (assert tunnel gone from
       Cloudflare), destroy the workspace, sign out, assert subsequent
       requests with the same session token return 401.
    """
    wait_for_env_ready(shared_env("default"))
    _ = (signup_email, fct_template_ref)
    raise AssertionError("not implemented yet -- see skip reason")
