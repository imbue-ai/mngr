"""Tests for the SSH provider config models."""

from pathlib import Path

from imbue.mngr.providers.ssh.config import SSHHostConfig


def test_with_expanded_key_file_expands_key_file_and_preserves_other_fields() -> None:
    config = SSHHostConfig(
        address="host",
        port=2200,
        user="me",
        key_file=Path("~/.ssh/id_ed25519"),
        known_hosts_file=Path("/etc/ssh/ssh_known_hosts"),
    )

    expanded = config.with_expanded_key_file()

    # key_file is expanded (no leading "~").
    assert expanded.key_file == Path("~/.ssh/id_ed25519").expanduser()
    assert not str(expanded.key_file).startswith("~")
    # Every other field is preserved, notably known_hosts_file.
    assert expanded.known_hosts_file == Path("/etc/ssh/ssh_known_hosts")
    assert expanded.address == "host"
    assert expanded.port == 2200
    assert expanded.user == "me"


def test_with_expanded_key_file_preserves_every_field_except_key_file() -> None:
    """Guard the core invariant: only key_file changes, so no field can be silently dropped
    even if SSHHostConfig gains new fields in the future."""
    config = SSHHostConfig(
        address="host",
        port=2200,
        user="me",
        key_file=Path("~/.ssh/id_ed25519"),
        known_hosts_file=Path("/etc/ssh/ssh_known_hosts"),
    )

    expanded = config.with_expanded_key_file()

    assert expanded.model_dump(exclude={"key_file"}) == config.model_dump(exclude={"key_file"})
    assert expanded.key_file == Path("~/.ssh/id_ed25519").expanduser()


def test_with_expanded_key_file_returns_unchanged_when_no_key_file() -> None:
    config = SSHHostConfig(address="host", known_hosts_file=Path("/etc/ssh/ssh_known_hosts"))

    expanded = config.with_expanded_key_file()

    assert expanded == config
    assert expanded.key_file is None
    assert expanded.known_hosts_file == Path("/etc/ssh/ssh_known_hosts")
