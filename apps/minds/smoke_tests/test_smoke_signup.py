"""The half of signup production lets automation drive: a minted account verifies its email and is offered the installers.

The signup form itself is behind Turnstile and the waitlist on production, so
the setup job mints the account; this test is the user's half -- sign in on the
accounts origin, verify the address through the real email path (mail.tm is the
mailbox), and see the download page list installers.
"""

from typing import Final

import httpx
import pytest
from loguru import logger

from imbue.minds.deployment_tests._mailtm import MailtmInbox
from imbue.minds.deployment_tests._mailtm import wait_for_verification_token_with_resends
from imbue.minds.smoke_tests.accounts import SMOKE_USER_AGENT
from imbue.minds.smoke_tests.accounts import origin_of
from imbue.minds.smoke_tests.data_types import SmokeBackend
from imbue.minds.smoke_tests.data_types import ThrowawayAccount

pytestmark = [pytest.mark.smoke]

_TEST_TIMEOUT_SECONDS: Final[int] = 900
_REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0
# Each round outlasts the connector's 60s per-user send cooldown, so a resend after a miss lands.
_VERIFICATION_EMAIL_WAIT_SECONDS: Final[float] = 90.0
_VERIFICATION_RESEND_ROUNDS: Final[int] = 4


def _resend_verification_email(client: httpx.Client, accounts_origin: str) -> None:
    resend = client.post(f"{accounts_origin}/accounts/api/send-verification")
    assert resend.status_code == 200, resend.text


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_the_throwaway_account_verifies_its_email_and_is_offered_the_installers(
    backend: SmokeBackend, throwaway_account: ThrowawayAccount, throwaway_inbox: MailtmInbox
) -> None:
    accounts_origin = origin_of(backend.accounts_origin)
    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": SMOKE_USER_AGENT}) as client:
        signin = client.post(
            f"{accounts_origin}/accounts/api/signin",
            json={"email": str(throwaway_account.email), "password": throwaway_account.password.get_secret_value()},
        )
        assert signin.status_code == 200, signin.text
        assert signin.json()["status"] == "OK", signin.text

        me_before = client.get(f"{accounts_origin}/accounts/api/me")
        assert me_before.status_code == 200, me_before.text
        assert me_before.json()["email"] == str(throwaway_account.email), me_before.text

        # A rerun of the same run id finds the address already verified; the
        # email path is only exercised while there is something to verify.
        if me_before.json()["email_verified"] is True:
            logger.info("{} is already verified; skipping the email round-trip", throwaway_account.email)
        else:
            sent = client.post(f"{accounts_origin}/accounts/api/send-verification")
            assert sent.status_code == 200, sent.text
            assert sent.json()["status"] == "OK", sent.text
            token = wait_for_verification_token_with_resends(
                throwaway_inbox,
                resend_verification_email=lambda: _resend_verification_email(client, accounts_origin),
                wait_seconds_per_round=_VERIFICATION_EMAIL_WAIT_SECONDS,
                round_count=_VERIFICATION_RESEND_ROUNDS,
            )
            verify = client.post(f"{accounts_origin}/accounts/api/verify-email", json={"token": str(token)})
            assert verify.status_code == 200, verify.text
            assert verify.json() == {"status": "OK"}, verify.text

        me_after = client.get(f"{accounts_origin}/accounts/api/me")
        assert me_after.status_code == 200, me_after.text
        assert me_after.json()["email_verified"] is True, me_after.text

        # The account is on the free plan, not the waitlist's guest plan, so the
        # download page answers it even while the waitlist is on.
        downloads = client.get(f"{accounts_origin}/accounts/api/downloads")
        assert downloads.status_code == 200, downloads.text
        platforms = downloads.json()["platforms"]
        assert platforms, downloads.text
        for entry in platforms:
            assert entry["url"].startswith("/download?platform="), entry
            assert entry["label"], entry
