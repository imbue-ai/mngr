import json
import shlex
import stat
from pathlib import Path

import pytest

from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.primitives import VaultSecretNotFoundError
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv


def _write_fake_vault(tmp_path: Path, body: str) -> Path:
    """Write an executable script masquerading as the ``vault`` CLI."""
    script = tmp_path / "vault"
    script.write_text("#!/usr/bin/env bash\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _make_split_vault_binary(tmp_path: Path, value_by_key: dict[str, str]) -> Path:
    """Fake ``vault`` for the split layout: ``kv list`` returns the keys, ``kv get`` returns each leaf's ``value``.

    Models the real two-call read path: a single ``kv list`` of the service
    directory followed by one ``kv get`` per child leaf, each holding a single
    ``value`` field.
    """
    list_path = tmp_path / "_list.json"
    list_path.write_text(json.dumps(sorted(value_by_key)))
    lines = [
        'sub="$2"',
        'path="${@: -1}"',
        'key="${path##*/}"',
        f'if [ "$sub" = "list" ]; then cat {shlex.quote(str(list_path))}; exit 0; fi',
        'if [ "$sub" = "get" ]; then',
    ]
    for key, value in value_by_key.items():
        leaf_path = tmp_path / f"_get_{key}.json"
        leaf_path.write_text(json.dumps({"data": {"data": {"value": value}}}))
        lines.append(f'  if [ "$key" = {shlex.quote(key)} ]; then cat {shlex.quote(str(leaf_path))}; exit 0; fi')
    lines.append('  echo "No value found" >&2; exit 2')
    lines.append("fi")
    lines.append('echo "unexpected vault invocation" >&2; exit 9')
    return _write_fake_vault(tmp_path, "\n".join(lines) + "\n")


def _make_branching_vault_binary(
    tmp_path: Path,
    *,
    list_stdout: str,
    list_exit: int = 0,
    list_stderr: str = "",
    get_stdout: str = "",
    get_exit: int = 0,
    get_stderr: str = "",
) -> Path:
    """Fake ``vault`` with independently-configurable ``kv list`` and ``kv get`` responses.

    Used by the error-path tests so we can, e.g., make ``kv list`` succeed but
    ``kv get`` return a malformed payload.
    """
    list_out = tmp_path / "_list_out.txt"
    list_err = tmp_path / "_list_err.txt"
    get_out = tmp_path / "_get_out.txt"
    get_err = tmp_path / "_get_err.txt"
    list_out.write_text(list_stdout)
    list_err.write_text(list_stderr)
    get_out.write_text(get_stdout)
    get_err.write_text(get_stderr)
    body = "\n".join(
        [
            'sub="$2"',
            f'if [ "$sub" = "list" ]; then cat {shlex.quote(str(list_out))}; '
            f"cat {shlex.quote(str(list_err))} >&2; exit {list_exit}; fi",
            f'if [ "$sub" = "get" ]; then cat {shlex.quote(str(get_out))}; '
            f"cat {shlex.quote(str(get_err))} >&2; exit {get_exit}; fi",
            'echo "unexpected vault invocation" >&2; exit 9',
        ]
    )
    return _write_fake_vault(tmp_path, body + "\n")


def test_read_vault_kv_happy_path(tmp_path: Path) -> None:
    fake = _make_split_vault_binary(tmp_path, {"CLOUDFLARE_API_TOKEN": "abc", "CLOUDFLARE_ZONE_ID": "def"})
    result = read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary=str(fake))
    assert result == {"CLOUDFLARE_API_TOKEN": "abc", "CLOUDFLARE_ZONE_ID": "def"}


def test_read_vault_kv_rejects_bad_prefix(tmp_path: Path) -> None:
    fake = _make_branching_vault_binary(tmp_path, list_stdout="[]")
    with pytest.raises(VaultReadError, match="must start with"):
        read_vault_kv(VaultPath("not/the/right/prefix"), vault_binary=str(fake))


def test_read_vault_kv_propagates_cli_failure(tmp_path: Path) -> None:
    # exit 1 (not 2) -> a generic/transient failure, which must stay a plain
    # VaultReadError, NOT the not-found subclass (so callers don't treat a
    # connectivity/auth blip as "secret absent").
    fake = _make_branching_vault_binary(
        tmp_path, list_stdout="", list_exit=1, list_stderr="Error making API request: timeout"
    )
    with pytest.raises(VaultReadError) as exc_info:
        read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary=str(fake))
    assert not isinstance(exc_info.value, VaultSecretNotFoundError)


def test_read_vault_kv_not_found_raises_secret_not_found(tmp_path: Path) -> None:
    """Vault CLI exit code 2 ("No value found") on the directory list -> VaultSecretNotFoundError.

    The distinct type lets deploy treat a genuinely-absent optional secret
    (e.g. a tier with no OVH entry) as empty without also swallowing transient
    failures.
    """
    fake = _make_branching_vault_binary(
        tmp_path, list_stdout="{}", list_exit=2, list_stderr="No value found at secrets/metadata/minds/dev/ovh"
    )
    with pytest.raises(VaultSecretNotFoundError):
        read_vault_kv(VaultPath("secrets/minds/dev/ovh"), vault_binary=str(fake))


def test_read_vault_kv_rejects_non_string_value_field(tmp_path: Path) -> None:
    fake = _make_branching_vault_binary(
        tmp_path,
        list_stdout='["CLOUDFLARE_API_TOKEN"]',
        get_stdout=json.dumps({"data": {"data": {"value": 42}}}),
    )
    with pytest.raises(VaultReadError, match="no string 'value' field"):
        read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary=str(fake))


def test_read_vault_kv_rejects_nested_directory(tmp_path: Path) -> None:
    """A child with a trailing slash is a nested dir, not a flat ``value`` leaf -> error."""
    fake = _make_branching_vault_binary(tmp_path, list_stdout='["nested/"]')
    with pytest.raises(VaultReadError, match="nested directory"):
        read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary=str(fake))


def test_read_vault_kv_missing_binary() -> None:
    """The reader surfaces a clear error when the configured CLI is absent."""
    # Use a name that won't exist on PATH and isn't an absolute path either.
    with pytest.raises(VaultReadError, match="not found on PATH"):
        read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary="vault-does-not-exist")


def test_read_vault_kv_malformed_leaf_data_shape(tmp_path: Path) -> None:
    """The reader rejects leaf payloads that don't have a ``data.data`` dict."""
    fake = _make_branching_vault_binary(
        tmp_path, list_stdout='["CLOUDFLARE_API_TOKEN"]', get_stdout='{"data": "not a dict"}'
    )
    with pytest.raises(VaultReadError, match="no data.data dict"):
        read_vault_kv(VaultPath("secrets/minds/dev/cloudflare"), vault_binary=str(fake))
