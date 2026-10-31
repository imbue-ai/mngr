import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests.helpers import SupertokensUserRecord
from imbue.minds.smoke_tests.primitives import PERSISTENT_ACCOUNT_EMAIL
from imbue.minds.smoke_tests.stale import is_sweepable_smoke_email
from imbue.minds.smoke_tests.stale import select_stale_smoke_users

_CUTOFF_MS = 1_000_000.0


def _user(user_id: str, email: str, time_joined_ms: int) -> SupertokensUserRecord:
    return SupertokensUserRecord(user_id=NonEmptyStr(user_id), emails=(email,), time_joined_ms=time_joined_ms)


@pytest.mark.parametrize(
    "email",
    [
        "studio-smoke-20261014t101500z-ab12cd34@example.mail.tm",
        "STUDIO-SMOKE-RUN-1@EXAMPLE.MAIL.TM",
        "  studio-smoke-run-1@example.mail.tm  ",
    ],
)
def test_a_run_s_throwaway_address_is_sweepable(email: str) -> None:
    assert is_sweepable_smoke_email(email) is True


@pytest.mark.parametrize(
    "email",
    [
        PERSISTENT_ACCOUNT_EMAIL,
        PERSISTENT_ACCOUNT_EMAIL.upper(),
        "someone@imbue.com",
        "test-20261014t101500z@example.test",
        "",
    ],
)
def test_the_persistent_account_and_every_real_account_are_never_sweepable(email: str) -> None:
    assert is_sweepable_smoke_email(email) is False


def test_only_smoke_accounts_older_than_the_cutoff_are_selected() -> None:
    users = (
        _user("old-smoke", "studio-smoke-run-1@example.mail.tm", int(_CUTOFF_MS) - 1),
        _user("fresh-smoke", "studio-smoke-run-2@example.mail.tm", int(_CUTOFF_MS) + 1),
        _user("persistent", PERSISTENT_ACCOUNT_EMAIL, 0),
        _user("real-user", "someone@imbue.com", 0),
    )

    stale = select_stale_smoke_users(users, cutoff_ms=_CUTOFF_MS)

    assert [str(user_id) for user_id, _email in stale] == ["old-smoke"]
    assert [str(email) for _user_id, email in stale] == ["studio-smoke-run-1@example.mail.tm"]


def test_an_account_with_no_email_at_all_is_skipped() -> None:
    users = (SupertokensUserRecord(user_id=NonEmptyStr("no-email"), emails=(), time_joined_ms=0),)
    assert select_stale_smoke_users(users, cutoff_ms=_CUTOFF_MS) == []


def test_an_account_whose_second_address_is_the_smoke_one_is_selected_by_it() -> None:
    users = (
        SupertokensUserRecord(
            user_id=NonEmptyStr("two-addresses"),
            emails=("someone@imbue.com", "studio-smoke-run-3@example.mail.tm"),
            time_joined_ms=0,
        ),
    )

    stale = select_stale_smoke_users(users, cutoff_ms=_CUTOFF_MS)

    assert [str(email) for _user_id, email in stale] == ["studio-smoke-run-3@example.mail.tm"]
