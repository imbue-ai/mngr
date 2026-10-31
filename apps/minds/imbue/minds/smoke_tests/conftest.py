"""Fixtures shared by the smoke-helper unit tests."""

import pytest
from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests.primitives import MailtmAccountId
from imbue.minds.deployment_tests.primitives import MailtmAddress
from imbue.minds.smoke_tests.data_types import SmokeRunHandoff
from imbue.minds.smoke_tests.data_types import ThrowawayAccount
from imbue.minds.smoke_tests.primitives import SmokeRunId


@pytest.fixture
def smoke_run_handoff() -> SmokeRunHandoff:
    """One run's handoff, shaped exactly as the setup command writes it."""
    throwaway_address = MailtmAddress("studio-smoke-20261014t101500z-ab12cd34@example.mail.tm")
    return SmokeRunHandoff(
        run_id=SmokeRunId("20261014t101500z"),
        tier=NonEmptyStr("staging"),
        throwaway=ThrowawayAccount(
            email=throwaway_address,
            password=SecretStr("Smoke-throwaway-1"),
            user_id=NonEmptyStr("2f3c4c1a-aaaa-bbbb-cccc-ddddeeeeffff"),
        ),
        mailtm_address=throwaway_address,
        mailtm_jwt=SecretStr("mailtm-jwt-value"),
        mailtm_account_id=MailtmAccountId("mailtm-account-id"),
    )
