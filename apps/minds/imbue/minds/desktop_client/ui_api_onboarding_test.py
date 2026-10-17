"""Tests for the /ui/api onboarding routes (the error-reporting consent answer + onboarding completion)."""

import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path


def test_consent_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.post("/ui/api/onboarding/consent")

    assert response.status_code == 401


def test_consent_without_an_answer_only_marks_the_screen_answered(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    assert minds_config.get_error_reporting_consent_given() is False
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/onboarding/consent")

    assert response.status_code == 200
    assert minds_config.get_error_reporting_consent_given() is True
    # The answer also (re)writes the latchkey daemon's live consent file.
    consent_path = latchkey_forward_sentry_consent_path(minds_config.data_dir)
    assert json.loads(consent_path.read_text())["report_unexpected_errors"] is True


@pytest.mark.witnesses(
    "home-page.consent-reporting-choice",
    partial="witnesses that the answer becomes the reporting setting; the checkbox starting checked is the SPA's "
    "(ConsentPage.test.ts), outside the Python witnessing surface",
)
@pytest.mark.parametrize("is_reporting_allowed", [True, False])
def test_the_consent_answer_becomes_the_reporting_setting(tmp_path: Path, is_reporting_allowed: bool) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/onboarding/consent", json={"report_unexpected_errors": is_reporting_allowed})

    assert response.status_code == 200
    assert minds_config.get_error_reporting_consent_given() is True
    assert minds_config.get_report_unexpected_errors() is is_reporting_allowed
    consent_path = latchkey_forward_sentry_consent_path(minds_config.data_dir)
    assert json.loads(consent_path.read_text())["report_unexpected_errors"] is is_reporting_allowed


@pytest.mark.parametrize(
    "body",
    [
        '{"report_unexpected_errors": "no"}',
        '{"report_unexpected_error": false}',
        '{"report_unexpected_errors": fals',
        "[false]",
    ],
    ids=["not-a-boolean", "unknown-field", "invalid-json", "not-an-object"],
)
def test_a_malformed_consent_answer_is_refused(tmp_path: Path, body: str) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/onboarding/consent", data=body, content_type="application/json")

    assert response.status_code == 400
    assert minds_config.get_error_reporting_consent_given() is False
    assert minds_config.get_report_unexpected_errors() is True


def test_complete_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.post("/ui/api/onboarding/complete")

    assert response.status_code == 401


def test_complete_persists_the_onboarding_flag(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    assert minds_config.get_is_onboarding_complete() is False
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/onboarding/complete")

    assert response.status_code == 200
    # Persisted, not per-run: a fresh config over the same directory reads it back.
    assert MindsConfig(data_dir=tmp_path / "minds-data").get_is_onboarding_complete() is True
