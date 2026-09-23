"""The request identity the desktop hands in-workspace services: the ``X-Imbue-Identity`` contract and its carrier.

A workspace service learns who is asking from one ``X-Imbue-Identity`` header
(see ``docs/design.md``). Over the local desktop forward the single
authenticated user is always the owner, so the desktop stamps
``{"owner":true}`` on every workspace's requests, and adds the owner's
account (``user_id`` and verified ``email``, as the plugin's session reports
them) for the workspaces that are shared, where the services care who the
owner is. ``mngr forward`` knows nothing of this contract: the desktop writes
it into the proxy's generic per-agent request-headers file
(``--request-headers-file``), which the proxy re-reads whenever it changes.

Which workspaces are shared comes from the sync service's records listing
(``shared_agent_ids``), applied after every workspace-record sync pass, and
from this desktop's own share enable/disable, applied immediately.
"""

import json
import threading
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_forward.request_headers import DEFAULT_AGENT_KEY
from imbue.mngr_forward.request_headers import RequestHeadersFile

IDENTITY_HEADER: Final[str] = "X-Imbue-Identity"
FORWARD_HEADERS_FILENAME: Final[str] = "forward_headers.json"
# CLEANUP: drop this (and ``remove_legacy_forward_identity_file``) once every
# desktop has run a build that writes ``forward_headers.json`` instead; the
# proxy stopped reading this file when ``--identity-file`` was removed.
_LEGACY_FORWARD_IDENTITY_FILENAME: Final[str] = "forward_identity.json"


class OwnerIdentity(FrozenModel):
    """The owner's account as the header carries it for a shared workspace."""

    user_id: str = Field(description="The account's user id (the stable identity)")
    email: str = Field(description="The account's email, as the plugin's session reports it")


@pure
def render_identity_header(identity: OwnerIdentity | None) -> str:
    """The compact JSON header value: ``{"owner":true}`` alone, or with the owner's ``user_id`` and ``email``.

    Over the local forward the requester is always the owner. The account is
    present exactly when the workspace is shared and its owning account is
    signed in here; a service tests key presence, so absent fields are omitted
    rather than nulled.
    """
    document: dict[str, object] = {"owner": True}
    if identity is not None:
        document["user_id"] = identity.user_id
        document["email"] = identity.email
    return json.dumps(document, separators=(",", ":"), ensure_ascii=True)


@pure
def render_forward_headers(identity_by_agent_id: Mapping[str, OwnerIdentity]) -> RequestHeadersFile:
    """The proxy's request-headers file: the owner flag for every agent, the owner's account for the shared ones."""
    entries = {DEFAULT_AGENT_KEY: {IDENTITY_HEADER: render_identity_header(None)}}
    for agent_id in sorted(identity_by_agent_id):
        entries[agent_id] = {IDENTITY_HEADER: render_identity_header(identity_by_agent_id[agent_id])}
    return RequestHeadersFile(headers_by_agent_key=entries)


def remove_legacy_forward_identity_file(data_dir: Path) -> None:
    """Delete the ``forward_identity.json`` an earlier desktop maintained for the proxy's removed ``--identity-file``."""
    legacy_path = data_dir / _LEGACY_FORWARD_IDENTITY_FILENAME
    try:
        legacy_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove the legacy forward identity file {}: {}", legacy_path, exc)


class ForwardHeadersFile(MutableModel):
    """Maintains the request-headers file ``mngr forward`` stamps ``X-Imbue-Identity`` from.

    Every write replaces the whole file atomically, so the proxy (which
    re-reads on any mtime or size change) never sees a torn document, and an
    unchanged rendering is not rewritten, so the proxy re-parses only on a
    real change.
    """

    path: Path = Field(frozen=True, description="The file passed to `mngr forward --request-headers-file`")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def write(self, identity_by_agent_id: Mapping[str, OwnerIdentity]) -> None:
        rendered = json.dumps(render_forward_headers(identity_by_agent_id).headers_by_agent_key, indent=2)
        with self._lock:
            if self._current_text() == rendered:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, rendered)

    def _current_text(self) -> str | None:
        try:
            return self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.debug("Could not read the forward headers file {}: {}", self.path, exc)
            return None


class ForwardIdentityPublisher(MutableModel):
    """Keeps the proxy's request-headers file in step with which workspaces are shared.

    The shared set per account is whatever the last workspace-record sync pass
    reported (:meth:`apply_sync_results`), edited immediately when this
    desktop enables or disables a share so the local view is right without
    waiting for the next pass. Every change rebuilds the file from the
    signed-in accounts' sessions; an account no longer signed in contributes
    nothing.
    """

    session_store: MultiAccountSessionStore = Field(frozen=True)
    headers_file: ForwardHeadersFile = Field(frozen=True)
    on_shared_workspaces_changed: Callable[[], None] | None = Field(
        default=None,
        frozen=True,
        description=(
            "Invoked after a local share enable, and by request_sync: kicks the sync scheduler so "
            "the next pass agrees with the local edit instead of a listing that predates it"
        ),
    )
    _shared_agent_ids_by_user_id: dict[str, set[str]] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def mark_shared(self, agent_id: str, user_id: str) -> None:
        """A share was just enabled here: the owner's account rides the workspace's local requests from now on.

        The share already exists at the connector when this is called, so the
        sync pass this requests agrees with the edit.
        """
        with self._lock:
            self._shared_agent_ids_by_user_id.setdefault(user_id, set()).add(agent_id)
        self.rebuild()
        self.request_sync()

    def mark_unshared(self, agent_id: str) -> None:
        """A share is being disabled here; the caller requests a sync once the connector share is gone."""
        with self._lock:
            for agent_ids in self._shared_agent_ids_by_user_id.values():
                agent_ids.discard(agent_id)
        self.rebuild()

    def request_sync(self) -> None:
        if self.on_shared_workspaces_changed is not None:
            self.on_shared_workspaces_changed()

    def apply_sync_results(self, shared_agent_ids_by_user_id: Mapping[str, Iterable[str]]) -> None:
        """Replace the shared set of every account listed (the ones whose records pull reached the connector)."""
        with self._lock:
            for user_id, agent_ids in shared_agent_ids_by_user_id.items():
                self._shared_agent_ids_by_user_id[user_id] = set(agent_ids)
        self.rebuild()

    def rebuild(self) -> None:
        """Write the file from the signed-in accounts' sessions (dropping accounts no longer signed in).

        Skipped when the account listing itself failed: an empty listing then
        means "unknown", and writing it would strip the owner's account from
        every shared workspace's requests until the next rebuild.
        """
        email_by_user_id = {str(account.user_id): account.email for account in self.session_store.list_accounts()}
        if not email_by_user_id and self.session_store.is_last_identity_read_failed:
            logger.warning("Leaving the forward headers file alone: the signed-in accounts could not be listed")
            return
        # The write stays under the lock so concurrent rebuilds (a sync pass on
        # its thread, a share toggle on a request thread) reach the file in
        # state order rather than leaving an older rendering last.
        with self._lock:
            for user_id in list(self._shared_agent_ids_by_user_id):
                if user_id not in email_by_user_id:
                    del self._shared_agent_ids_by_user_id[user_id]
            identity_by_agent_id = {
                agent_id: OwnerIdentity(user_id=user_id, email=email_by_user_id[user_id])
                for user_id, agent_ids in self._shared_agent_ids_by_user_id.items()
                for agent_id in agent_ids
            }
            self.headers_file.write(identity_by_agent_id)
