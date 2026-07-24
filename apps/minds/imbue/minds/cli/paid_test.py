"""Tests for the ``minds paid`` env-aware wrapper.

The argv construction is split into the pure ``build_admin_paid_email_args`` helper so
we can verify the contract without a subprocess; the click commands' env-activation
guard is exercised with :class:`click.testing.CliRunner`.
"""

import click
import pytest
from click.testing import CliRunner

from imbue.minds.cli.paid import admin_key_from_supertokens_secret
from imbue.minds.cli.paid import build_admin_paid_email_args
from imbue.minds.cli.paid import paid


def test_build_admin_paid_email_args_add() -> None:
    args = build_admin_paid_email_args(["add", "a@b.com"], connector_url="https://c.example/")
    assert args == [
        "imbue_cloud",
        "admin",
        "paid",
        "email",
        "add",
        "a@b.com",
        "--connector-url",
        "https://c.example/",
    ]


def test_build_admin_paid_email_args_list_paid_only() -> None:
    args = build_admin_paid_email_args(["list", "--paid-only"], connector_url="https://c.example/")
    assert args == [
        "imbue_cloud",
        "admin",
        "paid",
        "email",
        "list",
        "--paid-only",
        "--connector-url",
        "https://c.example/",
    ]


def test_admin_key_from_secret_prefers_new_field_over_deprecated() -> None:
    secret = {"MINDS_ADMIN_KEY": "new-key", "MINDS_PAID_ADMIN_KEY": "legacy-key"}
    assert admin_key_from_supertokens_secret(secret, "secret/minds/dev") == "new-key"


def test_admin_key_from_secret_falls_back_to_deprecated_field() -> None:
    secret = {"MINDS_ADMIN_KEY": "", "MINDS_PAID_ADMIN_KEY": "legacy-key"}
    assert admin_key_from_supertokens_secret(secret, "secret/minds/dev") == "legacy-key"


def test_admin_key_from_secret_raises_when_neither_field_set() -> None:
    with pytest.raises(click.ClickException, match="missing 'MINDS_ADMIN_KEY'"):
        admin_key_from_supertokens_secret({"MINDS_PAID_ADMIN_KEY": ""}, "secret/minds/dev")


def test_paid_add_requires_activated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no MINDS_ROOT_NAME set, the command must refuse early (before any Vault read)."""
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    result = CliRunner().invoke(paid, ["add", "someone@example.com"])
    assert result.exit_code != 0
    assert "No minds env is activated" in result.output
