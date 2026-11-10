"""The desktop's cache of other users' identity records.

The connector owns every user's identity record (id, verified email, display
name, profile picture). The desktop keeps ``identity_cache.json`` under its
data directory: records of other users it has resolved or looked up, so the
Share tab renders a user-id grant with a name and profile picture without a
connector round trip (entries older than a day are refreshed lazily).
"""

import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import UserIdentityCliInfo
from imbue.mngr.utils.file_utils import atomic_write

IDENTITY_CACHE_FILENAME: Final[str] = "identity_cache.json"

# A cached record is served as-is within this window and refreshed lazily
# (one connector lookup, best effort) once it is older.
IDENTITY_CACHE_TTL: Final[timedelta] = timedelta(days=1)


class IdentityRecord(FrozenModel):
    """One user's identity record as the connector serves it (email only when verified)."""

    user_id: str = Field(description="SuperTokens user id")
    email: str | None = Field(default=None, description="Verified email, or None when the connector reports none")
    display_name: str | None = Field(default=None, description="User-editable display name")
    profile_picture_url: str | None = Field(default=None, description="Public URL of the profile picture")


class CachedIdentityRecord(FrozenModel):
    """A cached record plus when it was fetched, for the lazy refresh."""

    record: IdentityRecord = Field(description="The record as last fetched")
    fetched_at: datetime = Field(description="When the record was fetched (UTC)")


class IdentityCacheDocument(FrozenModel):
    """The on-disk shape of the identity cache."""

    entries_by_user_id: dict[str, CachedIdentityRecord] = Field(default_factory=dict)


@pure
def record_from_cli_identity(info: UserIdentityCliInfo) -> IdentityRecord:
    """The record ``mngr imbue_cloud users show|resolve`` reported, as the desktop stores it."""
    return IdentityRecord(
        user_id=info.user_id,
        email=info.email,
        display_name=info.display_name,
        profile_picture_url=info.profile_picture_url,
    )


@pure
def _is_cached_record_fresh(entry: CachedIdentityRecord, now: datetime) -> bool:
    return now - entry.fetched_at < IDENTITY_CACHE_TTL


class IdentityCache(MutableModel):
    """The desktop's cache of other users' identity records, persisted as one JSON file."""

    path: Path = Field(frozen=True, description="The cache file (created on first write)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _load(self) -> IdentityCacheDocument:
        if not self.path.exists():
            return IdentityCacheDocument()
        try:
            return IdentityCacheDocument.model_validate_json(self.path.read_text())
        except (OSError, ValueError, ValidationError) as exc:
            logger.warning("Ignoring an unreadable identity cache at {}: {}", self.path, exc)
            return IdentityCacheDocument()

    def _save(self, document: IdentityCacheDocument) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, document.model_dump_json(indent=2))

    def get(self, user_id: str) -> CachedIdentityRecord | None:
        with self._lock:
            return self._load().entries_by_user_id.get(user_id)

    def put(self, record: IdentityRecord, now: datetime) -> None:
        with self._lock:
            document = self._load()
            entries = dict(document.entries_by_user_id)
            entries[record.user_id] = CachedIdentityRecord(record=record, fetched_at=now)
            self._save(IdentityCacheDocument(entries_by_user_id=entries))

    def get_or_fetch(
        self,
        user_id: str,
        # Looks the record up at the connector; None when the user is unknown.
        # An ImbueCloudCliError is served from the stale cached record when
        # there is one and raised otherwise.
        fetch: Callable[[str], IdentityRecord | None],
        now: datetime,
    ) -> IdentityRecord | None:
        """The cached record when fresh, else a fetched one (stale cache as the fallback)."""
        cached = self.get(user_id)
        if cached is not None and _is_cached_record_fresh(cached, now):
            return cached.record
        try:
            fetched = fetch(user_id)
        except ImbueCloudCliError as exc:
            if cached is None:
                raise
            logger.warning("Serving a stale identity record for {}: {}", user_id[:8], exc)
            return cached.record
        if fetched is not None:
            self.put(fetched, now)
            return fetched
        return cached.record if cached is not None else None


def now_utc() -> datetime:
    """The clock the identity cache's freshness window is measured against."""
    return datetime.now(timezone.utc)
