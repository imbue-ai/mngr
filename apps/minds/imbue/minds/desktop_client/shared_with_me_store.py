"""The desktop's copy of what is shared with each signed-in account (specs/shared-with-me/spec.md, 4.7 and 4.9).

Two things live here. The **cache** is the connector's listing as last
fetched per account (``<data_dir>/shared_with_me/<user_id>.json``): the
rows the home page lists, refreshed by the sync pass when the listing's
match set moved, when a pass was kicked, when the cache is absent, or when it is
older than ten minutes (the match set does not move on liveness). The **account
document** of kind ``shared_with_me`` holds what this side records per
share and follows the account to every device: the last access verdict an
open learned, and the links the user pasted by hand.

Everything here is discovery: the workspace's own grants file decides whether
an open is admitted, and the verdict is what that decision looked like.
"""

import hashlib
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from enum import auto
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.account_documents.data_types import AccountDocumentRecord
from imbue.minds.desktop_client.account_documents.data_types import DOCUMENT_STATE_ACTIVE
from imbue.minds.desktop_client.account_documents.data_types import DOCUMENT_STATE_DELETED
from imbue.minds.desktop_client.account_documents.store import ACCOUNT_DOCUMENTS_DIRNAME
from imbue.minds.desktop_client.account_documents.store import AccountDocumentStore
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudShareRefusedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import SharedWithMeListingCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import SharedWithMeMatchCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import SharedWithMeOwnerCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import SharedWithMeShareCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import SharedWithMeTargetCliInfo
from imbue.minds.desktop_client.share_targets import WHOLE_MACHINE_SERVICE
from imbue.minds.desktop_client.ui_models import UiSharedWorkspace
from imbue.minds.desktop_client.ui_models import UiSharedWorkspaceOwner
from imbue.minds.desktop_client.ui_models import UiSharedWorkspaceTarget
from imbue.minds.desktop_client.ui_models import UiWorkspaceEntry
from imbue.minds.errors import AccountDocumentRemovedElsewhereError
from imbue.minds.errors import AccountDocumentTooNewError
from imbue.minds.errors import SharedLinkRowMissingError
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_imbue_cloud.errors import SHARED_WITH_ME_UNAVAILABLE_CODE

SHARED_WITH_ME_DOCUMENT_KIND: Final[str] = "shared_with_me"
_CACHE_DIRNAME: Final[str] = "shared_with_me"
# How old a cached listing may get before a pass refetches it regardless of the
# match set: the match set ignores the share's liveness stamp, so this is how
# often the offline indicator can refresh on its own.
SHARED_WITH_ME_STALE_AFTER: Final[timedelta] = timedelta(minutes=10)
# The workspace route alias of a share key that is not an agent/host coordinate (frontend/src/models/sharedWithMe.ts).
SHARED_ROUTE_ALIAS_PREFIX: Final[str] = "shared-"

SHARED_WITH_ME_DOCUMENT_TOO_NEW_MESSAGE: Final[str] = (
    "This shared-workspace entry was written by a newer version of Imbue Studio and can only be changed there."
)


class SharedAccessVerdict(LowerCaseStrEnum):
    """What the last open of a shared workspace learned (lowercase wire values, specs/shared-with-me 4.7)."""

    ADMITTED = auto()
    # The gateway's "Not shared with you": the workspace's grants file does not admit the account.
    REFUSED = auto()
    # The gateway's "Sharing is misconfigured": the workspace cannot read its own grants file.
    MISCONFIGURED = auto()
    OFFLINE = auto()
    # The sign-in the frame goes through on the way in (the reverse handoff's redeem, specs/shared-with-me 4.8) did
    # not carry it onto the share: the frame landed on the accounts login page, or the redeem page refused it.
    SIGN_IN_FAILED = auto()


class SharedEntryKind(LowerCaseStrEnum):
    """Whether an account-document entry mirrors a listed share or a link the user pasted."""

    LISTED = auto()
    MANUAL = auto()


class SharedWithMeEntry(FrozenModel):
    """The typed metadata of one ``shared_with_me`` account document (spec 4.9)."""

    kind: SharedEntryKind = Field(description="A listed share's entry, or a pasted link's")
    link: str = Field(description="The share link the entry is about (the shell's for a listed share)")
    last_verdict: SharedAccessVerdict | None = Field(description="What the last open learned; None before any")
    last_verdict_at: datetime | None = Field(description="When the verdict was recorded")
    label: str = Field(description="What the tile shows for a manual link; empty for a listed share")

    def as_metadata(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "link": self.link,
            "last_verdict": self.last_verdict.value if self.last_verdict is not None else None,
            "last_verdict_at": self.last_verdict_at.isoformat() if self.last_verdict_at is not None else None,
            "label": self.label,
        }


class SharedWithMeCache(FrozenModel):
    """The connector's listing for one account as last fetched, with the match set it was built from."""

    matches: tuple[SharedWithMeMatchCliInfo, ...] = Field(description="The match set at the fetch")
    fetched_at: datetime = Field(description="When the listing was fetched (UTC)")
    shares: tuple[SharedWithMeShareCliInfo, ...] = Field(description="The rows, newest grant first")


class SharedWorkspaceRow(FrozenModel):
    """One row of 'Shared with me': a listed share joined with this side's entry, or a pasted link alone."""

    share_key: str = Field(description="The share's durable key; a manual link's document id")
    host_id: str = Field(description="The share's host id; empty for a manual link")
    workspace_domain: str = Field(description="The share's workspace domain; the pasted link's host for a manual one")
    workspace_name: str | None = Field(description="The workspace's name as the owner pushed it; None when unknown")
    owner: SharedWithMeOwnerCliInfo | None = Field(description="The owner; None for a manual link")
    status: str = Field(description="invited / accepted / declined; 'accepted' for a manual link")
    granted_via: str = Field(description="user / email / domain; 'unknown' for a manual link")
    last_seen_at: str | None = Field(description="The share's liveness stamp; None for a manual link")
    latest_grant_at: str = Field(description="When the newest grant naming the account was observed; '' when unknown")
    targets: tuple[SharedWithMeTargetCliInfo, ...] = Field(description="What may be opened, the shell first")
    verdict: SharedAccessVerdict | None = Field(description="What the last open learned; None before any")
    verdict_at: datetime | None = Field(description="When the verdict was recorded")
    is_manual: bool = Field(description="Whether the row is a pasted link rather than a listed share")

    @property
    def shell_link(self) -> str | None:
        """The link an open uses by default: the shell's, else the first target's."""
        for target in self.targets:
            if target.app == WHOLE_MACHINE_SERVICE and target.link:
                return target.link
        for target in self.targets:
            if target.link:
                return target.link
        return None

    def link_for_app(self, app: str | None) -> str | None:
        if app is None:
            return self.shell_link
        for target in self.targets:
            if target.app == app:
                return target.link
        return None


@pure
def normalize_manual_link(raw: str) -> str | None:
    """A pasted link as an https origin with a trailing slash, or None when it is not one."""
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    hostname = (parts.hostname or "").lower()
    if parts.scheme != "https" or not hostname or parts.username is not None or parts.password is not None:
        return None
    # A share origin is a name under a workspace domain, never an IP literal
    # (whose IPv6 form urlsplit strips the brackets of, leaving no valid URL).
    if ":" in hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    port_suffix = f":{port}" if port is not None and port != 443 else ""
    return f"https://{hostname}{port_suffix}/"


@pure
def manual_link_doc_id(link: str) -> str:
    """The document id of a pasted link: the first 40 hex characters of the link's sha256 (spec 4.9).

    A document id cannot carry a URL, and a bare hex prefix is what the SPA
    can route: a key that is not an ``agent-``/``host-`` coordinate travels as
    the ``shared-<key>`` route alias, whose id segment admits hex alone.
    """
    return hashlib.sha256(link.encode("utf-8")).hexdigest()[:40]


@pure
def _host_of_link(link: str) -> str:
    return (urlsplit(link).hostname or "").lower()


@pure
def entry_from_metadata(metadata: Mapping[str, object]) -> SharedWithMeEntry | None:
    """The typed entry a document's metadata holds, or None when it does not parse (logged by the caller)."""
    raw_verdict = metadata.get("last_verdict")
    raw_verdict_at = metadata.get("last_verdict_at")
    try:
        return SharedWithMeEntry(
            kind=SharedEntryKind(str(metadata.get("kind", SharedEntryKind.LISTED.value))),
            link=str(metadata.get("link", "")),
            last_verdict=SharedAccessVerdict(str(raw_verdict)) if raw_verdict else None,
            last_verdict_at=datetime.fromisoformat(str(raw_verdict_at)) if raw_verdict_at else None,
            label=str(metadata.get("label", "")),
        )
    except ValueError:
        return None


@pure
def _listed_row(share: SharedWithMeShareCliInfo, entry: SharedWithMeEntry | None) -> SharedWorkspaceRow:
    """A listed share joined with this side's entry for it (the verdict), if any."""
    return SharedWorkspaceRow(
        share_key=share.share_key,
        host_id=share.host_id,
        workspace_domain=share.workspace_domain,
        workspace_name=share.workspace_name,
        owner=share.owner,
        status=share.status,
        granted_via=share.granted_via,
        last_seen_at=share.last_seen_at,
        latest_grant_at=share.latest_grant_at,
        targets=share.targets,
        verdict=entry.last_verdict if entry is not None else None,
        verdict_at=entry.last_verdict_at if entry is not None else None,
        is_manual=False,
    )


@pure
def _manual_row(doc_id: str, entry: SharedWithMeEntry) -> SharedWorkspaceRow:
    """A pasted link's row: no owner, name, or status is known beyond what the user typed."""
    return SharedWorkspaceRow(
        share_key=doc_id,
        host_id="",
        # The pasted origin alone: the shell's header rewrite reaches exactly this host.
        workspace_domain=_host_of_link(entry.link),
        workspace_name=entry.label or None,
        owner=None,
        status="accepted",
        granted_via="unknown",
        last_seen_at=None,
        latest_grant_at="",
        targets=(
            SharedWithMeTargetCliInfo(app=WHOLE_MACHINE_SERVICE, display_name=entry.label or None, link=entry.link),
        ),
        verdict=entry.last_verdict,
        verdict_at=entry.last_verdict_at,
        is_manual=True,
    )


@pure
def merge_shared_rows(
    cache: SharedWithMeCache | None, entries_by_doc_id: Mapping[str, SharedWithMeEntry]
) -> list[SharedWorkspaceRow]:
    """The account's rows: every listed share with its verdict, then every pasted link no listed share covers.

    A pasted link whose host sits under a listed share's workspace domain is
    that share's (the listing row wins), so it is folded in rather than shown twice.
    """
    listed = list(cache.shares) if cache is not None else []
    rows = [_listed_row(share, entries_by_doc_id.get(share.share_key)) for share in listed]
    listed_domains = [share.workspace_domain.lower() for share in listed]
    for doc_id in sorted(entries_by_doc_id):
        entry = entries_by_doc_id[doc_id]
        if entry.kind is not SharedEntryKind.MANUAL:
            continue
        if any(_is_link_under(entry.link, domain) for domain in listed_domains):
            continue
        rows.append(_manual_row(doc_id, entry))
    return rows


@pure
def is_refresh_needed(
    cache: SharedWithMeCache | None, server_matches: Sequence[SharedWithMeMatchCliInfo] | None, now: datetime
) -> bool:
    """Whether a pass should refetch: no cache, a moved match set, or a cache older than the staleness window.

    ``server_matches`` is what the records pull carried; None (an older
    connector, or one that could not compute it) decides nothing.
    """
    if cache is None:
        return True
    if server_matches is not None and tuple(server_matches) != cache.matches:
        return True
    return now - cache.fetched_at >= SHARED_WITH_ME_STALE_AFTER


class SharedWithMeDocumentStore(AccountDocumentStore[AccountDocumentRecord]):
    """Owns the per-account replicas of the ``shared_with_me`` documents and their push/pull."""

    data_dir: Path = Field(frozen=True, description="Minds data dir (the replicas live under it)")

    def _document_kind(self) -> str:
        return SHARED_WITH_ME_DOCUMENT_KIND

    def _replica_dir(self) -> Path:
        return self.data_dir / ACCOUNT_DOCUMENTS_DIRNAME / SHARED_WITH_ME_DOCUMENT_KIND

    def _file_entries_key(self) -> str:
        return "documents"

    def _record_type(self) -> type[AccountDocumentRecord]:
        return AccountDocumentRecord

    def _too_new_error(self) -> AccountDocumentTooNewError:
        return AccountDocumentTooNewError(SHARED_WITH_ME_DOCUMENT_TOO_NEW_MESSAGE)

    def _removed_elsewhere_error(self) -> AccountDocumentRemovedElsewhereError:
        return AccountDocumentRemovedElsewhereError("The shared-workspace entry was removed on another device")

    def _is_payload_included_on_push(self) -> bool:
        # The entries are plaintext metadata only (a verdict and a link).
        return False

    def read_entries(self, user_id: str) -> dict[str, SharedWithMeEntry]:
        """Every active entry of the account by document id; a malformed one is skipped with a warning."""
        entries: dict[str, SharedWithMeEntry] = {}
        for record in self.list_records(user_id):
            if record.state != DOCUMENT_STATE_ACTIVE:
                continue
            entry = entry_from_metadata(record.metadata)
            if entry is None:
                logger.warning("Skipping a malformed shared_with_me entry {} of {}", record.doc_id, user_id[:8])
                continue
            entries[record.doc_id] = entry
        return entries

    def write_entry(self, user_id: str, doc_id: str, entry: SharedWithMeEntry) -> None:
        """Replace the account's entry under ``doc_id`` and mark it for the next push."""
        with self._lock:
            self._write_metadata_unlocked(user_id, doc_id, entry.as_metadata())

    def tombstone_entry(self, user_id: str, doc_id: str) -> None:
        """Mark the account's entry deleted for the next push (a no-op when there is none).

        Raises the kind's too-new error for an entry a newer app wrote, which this app must not rewrite.
        """
        with self._lock:
            self._load_unlocked()
            existing = self._records_by_user_id.get(user_id, {}).get(doc_id)
            if existing is None:
                return
            if self._is_record_too_new(existing):
                raise self._too_new_error()
            self._set_record_unlocked(
                user_id,
                existing.model_copy_update(
                    to_update(existing.field_ref().state, DOCUMENT_STATE_DELETED),
                    to_update(existing.field_ref().is_dirty, True),
                ),
            )


class SharedWithMeStore(MutableModel):
    """The per-account listing cache plus the account documents, and every read the home page and routes make."""

    data_dir: Path = Field(frozen=True, description="Minds data dir (the cache files live under it)")
    cli: ImbueCloudCli | None = Field(default=None, frozen=True, description="Transport; None disables fetches")
    documents: SharedWithMeDocumentStore = Field(frozen=True, description="The account documents of the kind")
    on_change: Callable[[], None] | None = Field(
        default=None,
        description=(
            "Invoked after every cache or entry write, so the channel publisher re-derives the workspace list; "
            "wired post-construction, once the publisher exists"
        ),
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _cache_by_user_id: dict[str, SharedWithMeCache] = PrivateAttr(default_factory=dict)
    _loaded_user_ids: set[str] = PrivateAttr(default_factory=set)
    # The accounts a cache file exists for, read off the directory once; None until then.
    _cached_user_ids: set[str] | None = PrivateAttr(default=None)

    def _cache_path(self, user_id: str) -> Path:
        return self.data_dir / _CACHE_DIRNAME / f"{user_id}.json"

    def _load_unlocked(self, user_id: str) -> SharedWithMeCache | None:
        if user_id in self._loaded_user_ids:
            return self._cache_by_user_id.get(user_id)
        self._loaded_user_ids.add(user_id)
        path = self._cache_path(user_id)
        if not path.exists():
            return None
        try:
            cache = SharedWithMeCache.model_validate_json(path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring an unreadable shared-with-me cache at {}: {}", path, exc)
            return None
        self._cache_by_user_id[user_id] = cache
        return cache

    def _save_unlocked(self, user_id: str, cache: SharedWithMeCache) -> None:
        self._cache_by_user_id[user_id] = cache
        self._loaded_user_ids.add(user_id)
        path = self._cache_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, cache.model_dump_json(indent=2))
        self._cached_user_ids_unlocked().add(user_id)

    def _cached_user_ids_unlocked(self) -> set[str]:
        if self._cached_user_ids is None:
            cache_dir = self.data_dir / _CACHE_DIRNAME
            self._cached_user_ids = {path.stem for path in cache_dir.glob("*.json")} if cache_dir.is_dir() else set()
        return self._cached_user_ids

    def _notify_change(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def read_cache(self, user_id: str) -> SharedWithMeCache | None:
        with self._lock:
            return self._load_unlocked(user_id)

    def refresh(self, user_id: str, account_email: str, now: datetime) -> bool:
        """Fetch the account's listing and replace its cache; False (the cache kept) when the fetch failed.

        A connector that does not serve the listing yet (spec section 7) is an
        empty listing, cached like any other so the pass stops asking until the
        staleness window; the refetch after it picks the connector's upgrade up.
        """
        if self.cli is None:
            return False
        try:
            listing = self.cli.list_shared_with_me(account=account_email)
        except ImbueCloudShareRefusedCliError as exc:
            if exc.code != SHARED_WITH_ME_UNAVAILABLE_CODE:
                logger.warning("Could not fetch what is shared with {}: {}", account_email, exc)
                return False
            logger.debug(
                "The connector does not list what is shared with {} yet; caching an empty listing", account_email
            )
            listing = SharedWithMeListingCliInfo(matches=(), shares=())
        except ImbueCloudCliError as exc:
            logger.warning("Could not fetch what is shared with {}: {}", account_email, exc)
            return False
        with self._lock:
            # Fetches overlap (the sync pass and the routes both refresh): one
            # that started before a newer one landed must not undo it.
            cached = self._load_unlocked(user_id)
            if cached is not None and cached.fetched_at > now:
                logger.debug("Keeping the newer shared-with-me listing of {} over a fetch from {}", user_id[:8], now)
                return True
            self._save_unlocked(
                user_id, SharedWithMeCache(matches=listing.matches, fetched_at=now, shares=listing.shares)
            )
        self._notify_change()
        return True

    def sync_documents(self, user_id: str, account_email: str) -> None:
        """Pull the account's entries and push the dirty ones; a pull that changed an entry wakes the publisher like a local write."""
        outcome = self.documents.pull(user_id, account_email)
        if outcome.is_reached:
            self.documents.push_dirty(user_id, account_email)
        if outcome.is_changed:
            self._notify_change()

    def refresh_if_needed(
        self,
        user_id: str,
        account_email: str,
        server_matches: Sequence[SharedWithMeMatchCliInfo] | None,
        is_forced: bool,
        now: datetime,
    ) -> bool:
        """Refresh when the match set moved, the cache is absent or stale, or the pass was kicked; True when fetched."""
        if not is_forced and not is_refresh_needed(self.read_cache(user_id), server_matches, now):
            return False
        return self.refresh(user_id, account_email, now)

    def apply_status(self, user_id: str, share_key: str, status: str) -> None:
        """Set a cached listed share's status, for a response the connector took but the refetch could not confirm.

        The match set is left as it was, so the next pass still refetches on the
        connector's moved match set; until then the cache says what was recorded.
        """
        with self._lock:
            cache = self._load_unlocked(user_id)
            if cache is None:
                return
            shares = tuple(
                share.model_copy_update(to_update(share.field_ref().status, status))
                if share.share_key == share_key
                else share
                for share in cache.shares
            )
            if shares == cache.shares:
                return
            self._save_unlocked(user_id, cache.model_copy_update(to_update(cache.field_ref().shares, shares)))
        self._notify_change()

    def drop_share(self, user_id: str, share_key: str) -> None:
        """Remove a cached listed share, for a report the connector took but the refetch could not confirm.

        A reported share is never listed again; the match set is left as it was,
        like ``apply_status``, so the next pass still refetches.
        """
        with self._lock:
            cache = self._load_unlocked(user_id)
            if cache is None:
                return
            shares = tuple(share for share in cache.shares if share.share_key != share_key)
            if shares == cache.shares:
                return
            self._save_unlocked(user_id, cache.model_copy_update(to_update(cache.field_ref().shares, shares)))
        self._notify_change()

    def forget_account(self, user_id: str) -> None:
        """Drop a signed-out account's cache (its documents are the record store's business)."""
        with self._lock:
            self._cache_by_user_id.pop(user_id, None)
            self._loaded_user_ids.discard(user_id)
            self._cached_user_ids_unlocked().discard(user_id)
            path = self._cache_path(user_id)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove the shared-with-me cache of {}: {}", user_id[:8], exc)
        self._notify_change()

    def list_rows(self, user_id: str) -> list[SharedWorkspaceRow]:
        return merge_shared_rows(self.read_cache(user_id), self.documents.read_entries(user_id))

    def find_row(self, user_id: str, share_key: str) -> SharedWorkspaceRow | None:
        for row in self.list_rows(user_id):
            if row.share_key == share_key:
                return row
        return None

    def recipient_account_id_of(self, any_id: str) -> str | None:
        """The account whose rows list a workspace route id (a share key, host id, workspace id, or the ``shared-`` alias).

        A window on a shared workspace's page belongs to the account the share
        was listed for, which no workspace record names.
        """
        wanted = (
            any_id[len(SHARED_ROUTE_ALIAS_PREFIX) :]
            if any_id.lower().startswith(SHARED_ROUTE_ALIAS_PREFIX)
            else any_id
        )
        # A pasted link lives in the account document alone, so an account whose
        # every fetch failed (no cache file yet) is still a recipient of its own entries.
        for user_id in sorted({*self.list_cached_user_ids(), *self.documents.list_user_ids()}):
            cache = self.read_cache(user_id)
            for share in cache.shares if cache is not None else ():
                if wanted in (share.share_key, share.host_id, share.workspace_id):
                    return user_id
            if any(row.is_manual and row.share_key == wanted for row in self.list_rows(user_id)):
                return user_id
        return None

    def list_cached_user_ids(self) -> list[str]:
        """Every account a cache file exists for, read off the directory once per call (the trust gate's coordinate
        lookups and the scheduler's pass ask)."""
        with self._lock:
            return sorted(self._cached_user_ids_unlocked())

    def listed_share_coordinates(self) -> frozenset[str]:
        """Every share key, host id, and workspace id of every account's listed shares, for the trust gate."""
        coordinates: set[str] = set()
        for user_id in self.list_cached_user_ids():
            cache = self.read_cache(user_id)
            for share in cache.shares if cache is not None else ():
                coordinates.add(share.share_key)
                coordinates.add(share.host_id)
                if share.workspace_id:
                    coordinates.add(share.workspace_id)
        return frozenset(coordinates)

    def record_verdict(self, user_id: str, share_key: str, verdict: SharedAccessVerdict, now: datetime) -> None:
        """Remember what an open learned, on the listed share's entry or the pasted link's.

        The shell reports the frame document's status on every full
        navigation inside the workspace, so a verdict the entry already holds
        is not written again: the document (and its push to the connector)
        changes only when the verdict does.
        """
        row = self.find_row(user_id, share_key)
        if row is None:
            raise SharedLinkRowMissingError(f"no listed share or pasted link of the account has the key {share_key}")
        existing = self.documents.read_entries(user_id).get(share_key)
        if existing is not None and existing.last_verdict is verdict:
            return
        entry = SharedWithMeEntry(
            kind=existing.kind if existing is not None else SharedEntryKind.LISTED,
            link=existing.link if existing is not None else (row.shell_link or ""),
            last_verdict=verdict,
            last_verdict_at=now,
            label=existing.label if existing is not None else "",
        )
        self.documents.write_entry(user_id, share_key, entry)
        self._notify_change()

    def clear_verdict(self, user_id: str, share_key: str) -> None:
        """Forget the last verdict (the retry), keeping a pasted link's entry otherwise intact."""
        existing = self.documents.read_entries(user_id).get(share_key)
        if existing is None:
            return
        self.documents.write_entry(
            user_id,
            share_key,
            existing.model_copy_update(
                to_update(existing.field_ref().last_verdict, None),
                to_update(existing.field_ref().last_verdict_at, None),
            ),
        )
        self._notify_change()

    def add_manual_link(self, user_id: str, link: str, label: str) -> SharedWorkspaceRow:
        """Record a pasted link (already normalized) and answer its row; a listed share's row when the link is under its domain."""
        covering = next(
            (row for row in self.list_rows(user_id) if _is_link_under(link, row.workspace_domain)),
            None,
        )
        if covering is not None:
            return covering
        doc_id = manual_link_doc_id(link)
        self.documents.write_entry(
            user_id,
            doc_id,
            SharedWithMeEntry(
                kind=SharedEntryKind.MANUAL, link=link, last_verdict=None, last_verdict_at=None, label=label
            ),
        )
        self._notify_change()
        row = self.find_row(user_id, doc_id)
        if row is None:
            raise SharedLinkRowMissingError(f"the pasted link {link} produced no row")
        return row

    def remove_manual_link(self, user_id: str, doc_id: str) -> bool:
        """Tombstone a pasted link's entry; False when the account has none under that id."""
        if doc_id not in self.documents.read_entries(user_id):
            return False
        self.documents.tombstone_entry(user_id, doc_id)
        self._notify_change()
        return True


@pure
def _is_link_under(link: str, workspace_domain: str) -> bool:
    hostname = (urlsplit(link).hostname or "").lower()
    domain = workspace_domain.lower()
    return bool(domain) and (hostname == domain or hostname.endswith("." + domain))


# The accent a shared row renders with before the owner's color is known (it never is: the share carries none).
SHARED_ROW_ACCENT: Final[str] = "#6b7280"


@pure
def shared_workspace_ui_entry(row: SharedWorkspaceRow, account_user_id: str, account_email: str) -> UiWorkspaceEntry:
    """The home page row for one share, listed under the recipient's account and flagged as not owned."""
    owner = (
        UiSharedWorkspaceOwner(
            user_id=row.owner.user_id,
            email=row.owner.email,
            display_name=row.owner.display_name,
            profile_picture_url=row.owner.profile_picture_url,
        )
        if row.owner is not None
        else UiSharedWorkspaceOwner(user_id="", email=None, display_name=None, profile_picture_url=None)
    )
    name = row.workspace_name or (row.owner.email if row.owner is not None and row.owner.email else None)
    return UiWorkspaceEntry(
        id=row.share_key,
        name=name or row.workspace_domain,
        accent=SHARED_ROW_ACCENT,
        host_id=row.host_id,
        account=account_email,
        account_user_id=account_user_id,
        is_owned=False,
        share=UiSharedWorkspace(
            share_key=row.share_key,
            host_id=row.host_id,
            workspace_domain=row.workspace_domain,
            owner=owner,
            status=row.status,
            granted_via=row.granted_via,
            last_seen_at=row.last_seen_at,
            latest_grant_at=row.latest_grant_at,
            targets=tuple(
                UiSharedWorkspaceTarget(
                    app=target.app,
                    display_name=target.display_name,
                    link=target.link,
                    granted_via=target.granted_via,
                    is_granted_as_app=target.is_granted_as_app,
                )
                for target in row.targets
            ),
            verdict=row.verdict.value if row.verdict is not None else None,
            verdict_at=row.verdict_at.isoformat() if row.verdict_at is not None else None,
            is_manual=row.is_manual,
        ),
    )


@pure
def shared_workspace_ui_entries(
    account_user_id: str, account_email: str, rows: Sequence[SharedWorkspaceRow]
) -> list[UiWorkspaceEntry]:
    return [shared_workspace_ui_entry(row, account_user_id, account_email) for row in rows]
