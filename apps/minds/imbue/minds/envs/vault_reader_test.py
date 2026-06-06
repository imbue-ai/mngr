import json
import stat
from pathlib import Path

import pytest

from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.primitives import VaultSecretNotFoundError
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import delete_vault_kv
from imbue.minds.envs.vault_reader import read_vault_kv


def _make_fake_vault_binary(tmp_path: Path, *, stdout: str, exit_code: int = 0, stderr: str = "") -> Path:
    """Create an executable script masquerading as the ``vault`` CLI.

    The script writes the precomputed stdout/stderr verbatim from sibling
    fixture files and exits with ``exit_code``, regardless of the args it
    receives. Tests substitute this in via the ``vault_binary`` parameter
    on ``read_vault_kv``.

    Writing the stdout/stderr to files (rather than inlining into the
    script body) sidesteps shell-escape pitfalls with payloads that
    contain quotes or here-doc terminators.
    """
    stdout_path = tmp_path / "_fake_vault_stdout.txt"
    stderr_path = tmp_path / "_fake_vault_stderr.txt"
    stdout_path.write_text(stdout)
    stderr_path.write_text(stderr)

    script = tmp_path / "vault"
    script.write_text(f"#!/usr/bin/env bash\ncat {stdout_path}\ncat {stderr_path} >&2\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_read_vault_kv_happy_path(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "data": {
                "data": {
                    "CLOUDFLARE_API_TOKEN": "abc",
                    "CLOUDFLARE_ZONE_ID": "def",
                }
            }
        }
    )
    fake = _make_fake_vault_binary(tmp_path, stdout=payload)
    result = read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary=str(fake))
    assert result == {"CLOUDFLARE_API_TOKEN": "abc", "CLOUDFLARE_ZONE_ID": "def"}


def test_read_vault_kv_rejects_bad_prefix(tmp_path: Path) -> None:
    fake = _make_fake_vault_binary(tmp_path, stdout="{}")
    with pytest.raises(VaultReadError, match="must start with"):
        read_vault_kv(VaultPath("not/the/right/prefix"), vault_binary=str(fake))


def test_read_vault_kv_propagates_cli_failure(tmp_path: Path) -> None:
    # exit 1 (not 2) -> a generic/transient failure, which must stay a plain
    # VaultReadError, NOT the not-found subclass (so callers don't treat a
    # connectivity/auth blip as "secret absent").
    fake = _make_fake_vault_binary(tmp_path, stdout="", exit_code=1, stderr="Error making API request: timeout")
    with pytest.raises(VaultReadError) as exc_info:
        read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary=str(fake))
    assert not isinstance(exc_info.value, VaultSecretNotFoundError)


def test_read_vault_kv_not_found_raises_secret_not_found(tmp_path: Path) -> None:
    """Vault CLI exit code 2 ("No value found") -> VaultSecretNotFoundError.

    The distinct type lets deploy treat a genuinely-absent optional secret
    (e.g. a tier with no OVH entry) as empty without also swallowing transient
    failures.
    """
    fake = _make_fake_vault_binary(
        tmp_path, stdout="", exit_code=2, stderr="No value found at secrets/data/minds/dev/ovh"
    )
    with pytest.raises(VaultSecretNotFoundError):
        read_vault_kv(VaultPath("secrets/minds/dev/ovh"), vault_binary=str(fake))


def test_read_vault_kv_rejects_non_string_values(tmp_path: Path) -> None:
    payload = json.dumps({"data": {"data": {"CLOUDFLARE_API_TOKEN": 42}}})
    fake = _make_fake_vault_binary(tmp_path, stdout=payload)
    with pytest.raises(VaultReadError, match="non-string value"):
        read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary=str(fake))


def test_read_vault_kv_missing_binary() -> None:
    """The reader surfaces a clear error when the configured CLI is absent."""
    # Use a name that won't exist on PATH and isn't an absolute path either.
    with pytest.raises(VaultReadError, match="not found on PATH"):
        read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary="vault-does-not-exist")


def test_read_vault_kv_malformed_data_shape(tmp_path: Path) -> None:
    """The reader rejects payloads that don't have a ``data.data`` dict."""
    fake = _make_fake_vault_binary(tmp_path, stdout='{"data": "not a dict"}')
    with pytest.raises(VaultReadError, match="no data.data dict"):
        read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary=str(fake))


def test_delete_vault_kv_idempotent_on_absent_entry(tmp_path: Path) -> None:
    # `vault kv metadata delete` returns the same "no value here" exit code
    # as `kv get` for an absent path -> treated as success so re-running
    # destroy after a partial failure is safe.
    fake = _make_fake_vault_binary(tmp_path, stdout="", exit_code=2, stderr="No value found at secrets/...")
    delete_vault_kv(VaultPath("secrets/minds/dev/generation"), vault_binary=str(fake))


def test_delete_vault_kv_propagates_real_failure_even_if_text_says_not_found(tmp_path: Path) -> None:
    # A non-2 exit is a genuine failure (auth/connectivity/permission). It
    # must surface even if its message happens to contain "not found" -- the
    # exact masking the old substring match allowed.
    fake = _make_fake_vault_binary(
        tmp_path, stdout="", exit_code=1, stderr="permission denied: secret path not found in policy"
    )
    with pytest.raises(VaultReadError, match="failed"):
        delete_vault_kv(VaultPath("secrets/minds/dev/generation"), vault_binary=str(fake))
