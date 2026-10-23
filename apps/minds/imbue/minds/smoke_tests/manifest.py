"""The run manifest: what a smoke test created, for the teardown job to clean up.

The test job holds only user credentials and never deletes anything, so it
appends one JSON line per resource it creates to the file named by
``MINDS_SMOKE_MANIFEST_PATH``. Teardown reads the file back -- tolerating its
absence, because a test process can die before it writes anything -- and
releases whatever the run leased.
"""

import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.minds.smoke_tests.data_types import SmokeResourceKind
from imbue.minds.smoke_tests.data_types import SmokeRunManifestEntry
from imbue.minds.smoke_tests.primitives import SMOKE_MANIFEST_PATH_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import SmokeRunId

# One manifest per run, so a teardown only ever reads its own run's entries.
MANIFEST_FILE_NAME_TEMPLATE: Final[str] = "run-manifest-{run_id}.jsonl"


def resolve_manifest_path_or_none() -> Path | None:
    """The manifest path the run was given, or None when it was given none."""
    raw_path = os.environ.get(SMOKE_MANIFEST_PATH_ENV_VAR)
    if not raw_path:
        return None
    return Path(raw_path)


@pure
def default_manifest_path(outputs_dir: Path, run_id: SmokeRunId) -> Path:
    """Where a run records its resources when the environment names no manifest."""
    return outputs_dir / MANIFEST_FILE_NAME_TEMPLATE.format(run_id=run_id)


def append_manifest_entry(path: Path, entry: SmokeRunManifestEntry) -> None:
    """Append one resource to the manifest, creating the file on first use.

    One line per append, flushed immediately: the teardown job must be able to
    read every resource a test created right up to the moment it died.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as manifest_file:
        manifest_file.write(entry.model_dump_json() + "\n")


def record_created_resource(path: Path, *, kind: SmokeResourceKind, identifier: str, owner_email: str) -> None:
    """Append the resource a test just created, stamped with the current time, so teardown can release it."""
    entry = SmokeRunManifestEntry(
        kind=kind,
        identifier=NonEmptyStr(identifier),
        owner_email=NonEmptyStr(owner_email),
        created_at=datetime.now(timezone.utc),
    )
    append_manifest_entry(path, entry)
    logger.info("Recorded {} {} of {} in the run manifest at {}", kind, identifier, owner_email, path)


def read_manifest_entries(path: Path) -> tuple[SmokeRunManifestEntry, ...]:
    """Every entry the manifest holds, or nothing at all when the file is absent."""
    if not path.is_file():
        logger.info("No smoke run manifest at {}; the run recorded no resources.", path)
        return ()
    return parse_manifest_entries(path.read_text(encoding="utf-8"))


@pure
def parse_manifest_entries(manifest_text: str) -> tuple[SmokeRunManifestEntry, ...]:
    """Parse the manifest's JSON lines, refusing a line that is not an entry.

    A malformed line is a hard error rather than a skip: it means a resource
    was created and recorded unreadably, which is exactly the resource that
    would leak if teardown shrugged it off.
    """
    entries: list[SmokeRunManifestEntry] = []
    for line_number, raw_line in enumerate(manifest_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entries.append(SmokeRunManifestEntry.model_validate_json(line))
        except ValueError as exc:
            raise SmokeConfigError(f"Malformed smoke run manifest line {line_number}: {line[:200]!r} ({exc})") from exc
    return tuple(entries)
