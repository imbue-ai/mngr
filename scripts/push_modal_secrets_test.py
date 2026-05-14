"""Unit tests for the pure helpers in scripts/push_modal_secrets.py."""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent / "push_modal_secrets.py"
_SPEC = importlib.util.spec_from_file_location("push_modal_secrets", _MODULE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
push_modal_secrets = importlib.util.module_from_spec(_SPEC)
sys.modules["push_modal_secrets"] = push_modal_secrets
_SPEC.loader.exec_module(push_modal_secrets)


def test_parse_template_keys_reads_exports(tmp_path: Path) -> None:
    """Template files are sh-shaped: ``export KEY=`` declares a key."""
    template = tmp_path / "cloudflare.sh"
    template.write_text(
        "# header comment\n"
        "export CLOUDFLARE_API_TOKEN=\n"
        "export CLOUDFLARE_ZONE_ID=\n"
        "\n"
        "# A comment with = embedded\n"
        "export CLOUDFLARE_DOMAIN=example.com\n"
    )
    keys = push_modal_secrets._parse_template_keys(tmp_path, "cloudflare")
    assert keys == ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_DOMAIN")


def test_parse_template_keys_ignores_bare_assignments(tmp_path: Path) -> None:
    """A line without an ``export`` prefix is still treated as a declaration."""
    template = tmp_path / "neon.sh"
    template.write_text("DATABASE_URL=\nexport AUTH_WEBSITE_DOMAIN=\n")
    keys = push_modal_secrets._parse_template_keys(tmp_path, "neon")
    assert "DATABASE_URL" in keys
    assert "AUTH_WEBSITE_DOMAIN" in keys


def test_parse_template_keys_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No template schema found"):
        push_modal_secrets._parse_template_keys(tmp_path, "nonexistent")


def test_validate_missing_keys_exits(tmp_path: Path) -> None:
    """``_validate_against_template`` raises SystemExit listing the missing keys."""
    with pytest.raises(SystemExit, match="is missing keys"):
        push_modal_secrets._validate_against_template(
            expected_keys=("A", "B"),
            vault_values={"A": "v"},
            service="cloudflare",
            vault_path="secrets/kv/minds/dev/cloudflare",  # type: ignore[arg-type]
        )


def test_validate_extras_are_allowed() -> None:
    """Extra Vault keys (not in the template) are silently dropped, not an error."""
    push_modal_secrets._validate_against_template(
        expected_keys=("A",),
        vault_values={"A": "v", "OPERATOR_NOTE": "ignore me"},
        service="cloudflare",
        vault_path="secrets/kv/minds/dev/cloudflare",  # type: ignore[arg-type]
    )
