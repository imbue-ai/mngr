"""Unit tests for tier generation-id lifecycle.

Each test substitutes a fake ``vault`` CLI (a shell script that
prints a precomputed payload + exit code) via the ``vault_binary``
kwarg, matching the pattern used in ``vault_reader_test.py``.
"""

import json
import re
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.generation import GENERATION_ID_KEY
from imbue.minds.envs.generation import delete_generation_id
from imbue.minds.envs.generation import ensure_generation_id
from imbue.minds.envs.generation import read_generation_id
from imbue.minds.envs.primitives import VaultReadError


def _make_fake_vault_binary(
    tmp_path: Path,
    *,
    get_stdout: str = "",
    get_exit_code: int = 0,
    get_stderr: str = "",
    put_exit_code: int = 0,
    put_stderr: str = "",
    delete_exit_code: int = 0,
    delete_stderr: str = "",
) -> Path:
    """Write a fake ``vault`` CLI that branches on the subcommand it's given.

    Records every invocation to ``<tmp_path>/_calls.log`` so tests can
    assert on the argv. Each subcommand has its own canned stdout /
    stderr / exit-code knob (``get_*`` for ``kv get``, ``put_*`` for
    ``kv put``, ``delete_*`` for ``kv metadata delete``).
    """
    get_stdout_path = tmp_path / "_get_stdout.txt"
    get_stderr_path = tmp_path / "_get_stderr.txt"
    put_stderr_path = tmp_path / "_put_stderr.txt"
    delete_stderr_path = tmp_path / "_delete_stderr.txt"
    get_stdout_path.write_text(get_stdout)
    get_stderr_path.write_text(get_stderr)
    put_stderr_path.write_text(put_stderr)
    delete_stderr_path.write_text(delete_stderr)

    log_path = tmp_path / "_calls.log"
    script = tmp_path / "vault"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log_path}\n'
        'if [[ "$1" == "kv" && "$2" == "get" ]]; then\n'
        f"  cat {get_stdout_path}\n"
        f"  cat {get_stderr_path} >&2\n"
        f"  exit {get_exit_code}\n"
        'elif [[ "$1" == "kv" && "$2" == "put" ]]; then\n'
        f"  cat {put_stderr_path} >&2\n"
        f"  exit {put_exit_code}\n"
        'elif [[ "$1" == "kv" && "$2" == "metadata" && "$3" == "delete" ]]; then\n'
        f"  cat {delete_stderr_path} >&2\n"
        f"  exit {delete_exit_code}\n"
        "else\n"
        '  echo "unexpected vault invocation: $*" >&2\n'
        "  exit 99\n"
        "fi\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def _root_cg() -> "Iterator[ConcurrencyGroup]":
    """Yield an active ConcurrencyGroup so the spawned ``vault`` subprocess
    can run under it (``cg.run_process_to_completion`` requires ACTIVE state).
    """
    cg = ConcurrencyGroup(name="generation-test-root")
    with cg:
        yield cg


def _kv_get_payload(*, generation_id: str | None) -> str:
    inner = {GENERATION_ID_KEY: generation_id} if generation_id is not None else {}
    return json.dumps({"data": {"data": inner}})


def test_read_generation_id_returns_value_when_present(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    fake = _make_fake_vault_binary(tmp_path, get_stdout=_kv_get_payload(generation_id="gen-abc"))
    result = read_generation_id(
        "secrets/minds/staging",
        parent_concurrency_group=_root_cg,
        vault_binary=str(fake),
    )
    assert result == "gen-abc"


def test_read_generation_id_returns_none_when_entry_missing(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    fake = _make_fake_vault_binary(
        tmp_path,
        get_stdout="",
        get_exit_code=2,
        get_stderr="No value found at secrets/data/minds/staging/generation",
    )
    assert (
        read_generation_id(
            "secrets/minds/staging",
            parent_concurrency_group=_root_cg,
            vault_binary=str(fake),
        )
        is None
    )


def test_read_generation_id_raises_when_key_absent_from_present_entry(
    tmp_path: Path, _root_cg: ConcurrencyGroup
) -> None:
    # The entry exists but has no MINDS_TIER_GENERATION_ID key (a partial
    # write / corruption). This must surface, NOT be treated as "no id yet"
    # (which would mint a fresh id over the malformed entry).
    fake = _make_fake_vault_binary(tmp_path, get_stdout=_kv_get_payload(generation_id=None))
    with pytest.raises(VaultReadError, match="malformed"):
        read_generation_id(
            "secrets/minds/staging",
            parent_concurrency_group=_root_cg,
            vault_binary=str(fake),
        )


def test_read_generation_id_propagates_unexpected_vault_error(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    # A non-2 exit is a transient / auth / connectivity failure (NOT
    # "absent"). read_vault_kv raises a plain VaultReadError for it, which
    # read_generation_id must let propagate rather than treat as "no id yet".
    fake = _make_fake_vault_binary(
        tmp_path,
        get_exit_code=1,
        get_stderr="permission denied",
    )
    with pytest.raises(VaultReadError, match="permission denied"):
        read_generation_id(
            "secrets/minds/staging",
            parent_concurrency_group=_root_cg,
            vault_binary=str(fake),
        )


def test_ensure_generation_id_returns_existing_value_without_writing(
    tmp_path: Path, _root_cg: ConcurrencyGroup
) -> None:
    fake = _make_fake_vault_binary(tmp_path, get_stdout=_kv_get_payload(generation_id="already-there"))
    result = ensure_generation_id(
        "secrets/minds/staging",
        parent_concurrency_group=_root_cg,
        vault_binary=str(fake),
    )
    assert result == "already-there"
    calls = (tmp_path / "_calls.log").read_text()
    assert "kv put" not in calls


def test_ensure_generation_id_mints_and_writes_when_missing(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    fake = _make_fake_vault_binary(
        tmp_path,
        get_exit_code=2,
        get_stderr="No value found at secrets/data/minds/staging/generation",
    )
    result = ensure_generation_id(
        "secrets/minds/staging",
        parent_concurrency_group=_root_cg,
        vault_binary=str(fake),
    )
    # uuid4().hex is a 32-character lowercase hex string.
    assert re.fullmatch(r"[0-9a-f]{32}", result)
    calls = (tmp_path / "_calls.log").read_text()
    assert "kv put" in calls
    assert f"{GENERATION_ID_KEY}={result}" in calls


def test_delete_generation_id_is_idempotent_on_missing_entry(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    fake = _make_fake_vault_binary(
        tmp_path,
        delete_exit_code=2,
        delete_stderr="No value found at secrets/metadata/minds/staging/generation",
    )
    # Should not raise -- "not found" is treated as success.
    delete_generation_id(
        "secrets/minds/staging",
        parent_concurrency_group=_root_cg,
        vault_binary=str(fake),
    )


def test_delete_generation_id_invokes_vault_metadata_delete(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    fake = _make_fake_vault_binary(tmp_path)
    delete_generation_id(
        "secrets/minds/staging",
        parent_concurrency_group=_root_cg,
        vault_binary=str(fake),
    )
    calls = (tmp_path / "_calls.log").read_text()
    assert "kv metadata delete" in calls
    assert "minds/staging/generation" in calls
