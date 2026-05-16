"""Tests for ``mngr imbue_cloud admin pool ...`` provider-generic helpers.

We don't exercise the full subprocess pipeline (that needs a real OVH
provider + a real Neon DB); instead we cover the small pure helpers
that encode the contract this command commits to: tag-value validation,
ufw command ordering, and the CLI signature.
"""

from typing import Any

import click
import pytest
from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.admin import _CONTAINER_SSH_PORT
from imbue.mngr_imbue_cloud.cli.admin import _ufw_provision_commands
from imbue.mngr_imbue_cloud.cli.admin import build_extra_tags_env_value
from imbue.mngr_imbue_cloud.cli.admin import pool


def test_build_extra_tags_env_value_empty() -> None:
    assert build_extra_tags_env_value(()) == ""


def test_build_extra_tags_env_value_single_entry() -> None:
    assert build_extra_tags_env_value(("minds_env=alice",)) == "minds_env=alice"


def test_build_extra_tags_env_value_multiple_entries_join_with_comma() -> None:
    assert build_extra_tags_env_value(("minds_env=alice", "pool-owner=bob")) == "minds_env=alice,pool-owner=bob"


def test_build_extra_tags_env_value_rejects_entry_without_equals() -> None:
    with pytest.raises(click.UsageError, match="KEY=VALUE"):
        build_extra_tags_env_value(("minds_env=alice", "no-equals"))


def test_ufw_provision_commands_allows_port_22_before_enable() -> None:
    """ufw enable must come *after* allow 22, otherwise the in-progress SSH session is killed."""
    commands = _ufw_provision_commands(_CONTAINER_SSH_PORT)
    allow_22_index = next(i for i, c in enumerate(commands) if c == "ufw allow 22/tcp")
    enable_index = next(i for i, c in enumerate(commands) if c == "ufw --force enable")
    assert allow_22_index < enable_index


def test_ufw_provision_commands_allows_container_ssh_port() -> None:
    commands = _ufw_provision_commands(_CONTAINER_SSH_PORT)
    assert f"ufw allow {_CONTAINER_SSH_PORT}/tcp" in commands


def test_ufw_provision_commands_installs_ufw_first() -> None:
    """ufw must be installed before any ufw allow / enable can run."""
    commands = _ufw_provision_commands(_CONTAINER_SSH_PORT)
    install_index = next(i for i, c in enumerate(commands) if "install -y ufw" in c)
    first_ufw_use = next(i for i, c in enumerate(commands) if c.startswith("ufw "))
    assert install_index < first_ufw_use


def test_ufw_provision_commands_sets_default_deny_incoming() -> None:
    commands = _ufw_provision_commands(_CONTAINER_SSH_PORT)
    assert "ufw default deny incoming" in commands
    assert "ufw default allow outgoing" in commands


def test_pool_create_requires_region() -> None:
    runner = CliRunner()
    result = runner.invoke(
        pool,
        [
            "create",
            "--count",
            "1",
            "--attributes",
            "{}",
            "--workspace-dir",
            ".",
            "--management-public-key-file",
            "/dev/null",
            "--database-url",
            "postgres://example",
        ],
    )
    assert result.exit_code != 0
    assert "Missing option" in result.output and "--region" in result.output


def test_pool_create_rejects_malformed_tag(tmp_path: Any) -> None:
    """A ``--tag`` value without ``=`` aborts the bake before any subprocess work."""
    runner = CliRunner()
    key_file = tmp_path / "mgmt.pub"
    key_file.write_text("ssh-ed25519 AAAA... operator@host\n")
    result = runner.invoke(
        pool,
        [
            "create",
            "--count",
            "1",
            "--region",
            "US-EAST-VA",
            "--tag",
            "no-equals",
            "--attributes",
            "{}",
            "--workspace-dir",
            str(tmp_path),
            "--management-public-key-file",
            str(key_file),
            "--database-url",
            "postgres://example",
        ],
    )
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output or "KEY=VALUE" in str(result.exception)
