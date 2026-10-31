from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.smoke_tests.data_types import SmokeResourceKind
from imbue.minds.smoke_tests.data_types import SmokeRunManifestEntry
from imbue.minds.smoke_tests.manifest import append_manifest_entry
from imbue.minds.smoke_tests.manifest import default_manifest_path
from imbue.minds.smoke_tests.manifest import parse_manifest_entries
from imbue.minds.smoke_tests.manifest import read_manifest_entries
from imbue.minds.smoke_tests.manifest import record_created_resource
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import SmokeRunId


def _entry(kind: SmokeResourceKind, identifier: str) -> SmokeRunManifestEntry:
    return SmokeRunManifestEntry(
        kind=kind,
        identifier=NonEmptyStr(identifier),
        owner_email=NonEmptyStr("studio-smoke-persistent@example.test"),
        created_at=datetime(2026, 10, 14, 10, 15, tzinfo=timezone.utc),
    )


def test_appended_entries_read_back_in_order(tmp_path: Path) -> None:
    manifest_path = tmp_path / "nested" / "manifest.jsonl"
    append_manifest_entry(manifest_path, _entry(SmokeResourceKind.WORKSPACE, "host-db-id-1"))
    append_manifest_entry(manifest_path, _entry(SmokeResourceKind.SHARE, "host-id-1"))

    entries = read_manifest_entries(manifest_path)

    assert [entry.kind for entry in entries] == [SmokeResourceKind.WORKSPACE, SmokeResourceKind.SHARE]
    assert [str(entry.identifier) for entry in entries] == ["host-db-id-1", "host-id-1"]
    assert entries[0].created_at == datetime(2026, 10, 14, 10, 15, tzinfo=timezone.utc)


def test_a_manifest_the_test_job_never_wrote_reads_as_empty(tmp_path: Path) -> None:
    assert read_manifest_entries(tmp_path / "absent.jsonl") == ()


def test_blank_lines_are_tolerated() -> None:
    line = _entry(SmokeResourceKind.WORKSPACE, "host-db-id-1").model_dump_json()
    assert len(parse_manifest_entries(f"\n{line}\n\n")) == 1


def test_a_malformed_line_is_refused_rather_than_skipped() -> None:
    line = _entry(SmokeResourceKind.WORKSPACE, "host-db-id-1").model_dump_json()
    with pytest.raises(SmokeConfigError, match="Malformed smoke run manifest line 2"):
        parse_manifest_entries(f"{line}\n{{not json}}\n")


def test_a_line_that_is_json_but_not_an_entry_is_refused() -> None:
    with pytest.raises(SmokeConfigError, match="Malformed smoke run manifest line 1"):
        parse_manifest_entries('{"kind": "WORKSPACE"}\n')


def test_recording_a_created_resource_stamps_it_and_appends_it(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    before = datetime.now(timezone.utc)
    record_created_resource(
        manifest_path,
        kind=SmokeResourceKind.SHARE,
        identifier="host-abc",
        owner_email="studio-smoke-persistent@example.test",
    )

    (entry,) = read_manifest_entries(manifest_path)
    assert entry.kind == SmokeResourceKind.SHARE
    assert str(entry.identifier) == "host-abc"
    assert str(entry.owner_email) == "studio-smoke-persistent@example.test"
    assert entry.created_at >= before
    assert entry.created_at.tzinfo is not None


def test_the_default_manifest_is_one_file_per_run(tmp_path: Path) -> None:
    first = default_manifest_path(tmp_path, SmokeRunId("20261015t230827z"))
    second = default_manifest_path(tmp_path, SmokeRunId("20261016t010203z"))

    assert first == tmp_path / "run-manifest-20261015t230827z.jsonl"
    assert first != second
