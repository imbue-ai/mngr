import pytest
from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.paid import _resolve_admin_api_key
from imbue.mngr_imbue_cloud.cli.paid import paid


def test_paid_group_lists_domain_and_email_subgroups() -> None:
    result = CliRunner().invoke(paid, ["--help"])
    assert result.exit_code == 0
    assert "domain" in result.output
    assert "email" in result.output


@pytest.mark.parametrize("subgroup", ["domain", "email"])
def test_paid_subgroups_expose_add_remove_list(subgroup: str) -> None:
    result = CliRunner().invoke(paid, [subgroup, "--help"])
    assert result.exit_code == 0
    for name in ("add", "remove", "list"):
        assert name in result.output


def test_resolve_admin_api_key_prefers_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", "from-env")
    assert _resolve_admin_api_key("from-flag").get_secret_value() == "from-flag"


def test_resolve_admin_api_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", "from-env")
    assert _resolve_admin_api_key(None).get_secret_value() == "from-env"


def test_resolve_admin_api_key_errors_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDS_PAID_ADMIN_KEY", raising=False)
    with pytest.raises(SystemExit):
        _resolve_admin_api_key(None)


def test_domain_add_requires_connector_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the key set but no connector URL configured, the command fails (no network call)."""
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", "k")
    monkeypatch.delenv("MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL", raising=False)
    result = CliRunner().invoke(paid, ["domain", "add", "imbue.com"])
    assert result.exit_code != 0
