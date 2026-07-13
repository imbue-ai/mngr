import json
from pathlib import Path

from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.sync import sync


def test_sync_group_lists_subcommands() -> None:
    result = CliRunner().invoke(sync, ["--help"])
    assert result.exit_code == 0
    for name in ("records", "bundle", "scrub-secrets"):
        assert name in result.output


def test_sync_records_group_lists_subcommands() -> None:
    result = CliRunner().invoke(sync, ["records", "--help"])
    assert result.exit_code == 0
    for name in ("pull", "push"):
        assert name in result.output


def test_sync_bundle_group_lists_subcommands() -> None:
    result = CliRunner().invoke(sync, ["bundle", "--help"])
    assert result.exit_code == 0
    for name in ("pull", "push", "delete"):
        assert name in result.output


def test_records_push_rejects_non_json_stdin() -> None:
    result = CliRunner().invoke(sync, ["records", "push"], input="not json at all")
    assert result.exit_code == 2
    assert "not valid JSON" in result.output


def test_records_push_rejects_invalid_record() -> None:
    result = CliRunner().invoke(sync, ["records", "push"], input=json.dumps({"host_id": "h"}))
    assert result.exit_code == 2
    assert "invalid workspace record" in result.output


def test_bundle_push_rejects_invalid_bundle() -> None:
    result = CliRunner().invoke(sync, ["bundle", "push"], input=json.dumps({"kdf_salt": "x"}))
    assert result.exit_code == 2
    assert "invalid key bundle" in result.output


def test_records_push_rejects_unreadable_input_file(tmp_path: Path) -> None:
    result = CliRunner().invoke(sync, ["records", "push", "--input-file", str(tmp_path / "missing.json")])
    assert result.exit_code == 2
    assert "could not read --input-file" in result.output


def test_records_push_reads_payload_from_input_file(tmp_path: Path) -> None:
    # A payload that parses as JSON but fails record validation proves the file
    # branch was read and parsed while stopping before any network call.
    payload_path = tmp_path / "record.json"
    payload_path.write_text(json.dumps({"host_id": "h"}))
    result = CliRunner().invoke(sync, ["records", "push", "--input-file", str(payload_path)])
    assert result.exit_code == 2
    assert "invalid workspace record" in result.output
