"""Tests for the /ui/api onboarding routes (consent acknowledgement + skip account setup)."""

import json
from pathlib import Path

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.state import get_state
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path


def test_consent_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.post("/ui/api/onboarding/consent")

    assert response.status_code == 401


def test_consent_marks_the_notice_acknowledged(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    assert minds_config.get_error_reporting_consent_given() is False
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/onboarding/consent")

    assert response.status_code == 200
    assert minds_config.get_error_reporting_consent_given() is True
    # The acknowledgement also (re)writes the latchkey daemon's live consent
    # file, matching the legacy POST /consent handler.
    consent_path = latchkey_forward_sentry_consent_path(minds_config.data_dir)
    assert json.loads(consent_path.read_text())["report_unexpected_errors"] is True


def test_skip_account_setup_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.post("/ui/api/onboarding/skip-account-setup")

    assert response.status_code == 401


def test_skip_account_setup_sets_the_run_flag(tmp_path: Path) -> None:
    client, app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    with app.app_context():
        assert get_state(app).is_account_setup_skipped is False

    response = client.post("/ui/api/onboarding/skip-account-setup")

    assert response.status_code == 200
    with app.app_context():
        assert get_state(app).is_account_setup_skipped is True
