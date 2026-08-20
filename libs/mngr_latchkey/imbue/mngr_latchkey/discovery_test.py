"""Unit tests for the remote-state watchdog handler in :mod:`imbue.mngr_latchkey.discovery`.

Drives :class:`_LatchkeyStateChangeHandler.dispatch` directly with
synthetic watchdog events, the same way the observer's emitter thread
would, so the event-type allowlist and path matching are covered without
spawning a real observer or touching inotify.

The critical regression covered here: watchdog's Linux (inotify) observer
dispatches read-lifecycle events (``FileOpenedEvent`` / ``FileClosedNoWriteEvent``)
for every *read* of a watched file, and the sync callbacks themselves read
the watched files -- so a handler that reacted to those events re-triggered
itself forever (a full VPS re-sync every ~6s for the supervisor's lifetime).
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from loguru import logger
from watchdog.events import DirModifiedEvent
from watchdog.events import FileClosedEvent
from watchdog.events import FileClosedNoWriteEvent
from watchdog.events import FileCreatedEvent
from watchdog.events import FileDeletedEvent
from watchdog.events import FileModifiedEvent
from watchdog.events import FileMovedEvent
from watchdog.events import FileOpenedEvent

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import HostId
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import LATCHKEY_CREDENTIAL_TYPE_OAUTH
from imbue.mngr_latchkey.core import LATCHKEY_CREDENTIAL_TYPE_ZOOM_SERVER_TO_SERVER
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.discovery import LatchkeyDiscoveryHandler
from imbue.mngr_latchkey.discovery import _LatchkeyStateChangeHandler
from imbue.mngr_latchkey.discovery import _is_renewal_pass_due
from imbue.mngr_latchkey.discovery import _seconds_since
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.testing import FakeLatchkey
from imbue.mngr_latchkey.testing import make_full_fake_latchkey


class _SyncRecorder(MutableModel):
    """Records the callback invocations the handler under test fires."""

    credential_sync_count: int = 0
    permission_sync_host_ids: list[str] = []

    def on_credentials_changed(self) -> None:
        self.credential_sync_count += 1

    def on_host_permissions_changed(self, host_id_str: str) -> None:
        self.permission_sync_host_ids.append(host_id_str)


def _build_handler(tmp_path: Path, host_id: HostId, recorder: _SyncRecorder) -> _LatchkeyStateChangeHandler:
    return _LatchkeyStateChangeHandler(
        credentials_path=tmp_path / "credentials.json.enc",
        plugin_data_dir=tmp_path / "mngr_latchkey",
        known_remote_host_ids=lambda: frozenset({str(host_id)}),
        on_credentials_changed=recorder.on_credentials_changed,
        on_host_permissions_changed=recorder.on_host_permissions_changed,
    )


def test_read_lifecycle_events_do_not_trigger_any_sync(tmp_path: Path) -> None:
    """A pure read of a watched file (open + close-no-write) must be inert.

    This is the feedback-loop regression: the sync callbacks read the very
    files being watched, so reacting to read events re-triggers the sync
    forever.
    """
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    credentials_path = str(tmp_path / "credentials.json.enc")
    permissions_path = str(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id))

    for path in (credentials_path, permissions_path):
        handler.dispatch(FileOpenedEvent(path))
        handler.dispatch(FileClosedNoWriteEvent(path))

    assert recorder.credential_sync_count == 0
    assert recorder.permission_sync_host_ids == []


def test_close_after_write_event_does_not_double_fire(tmp_path: Path) -> None:
    """``FileClosedEvent`` (IN_CLOSE_WRITE) is excluded: the accompanying
    ``FileModifiedEvent`` already triggers the sync, so reacting to both
    would double every sync."""
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)

    handler.dispatch(FileClosedEvent(str(tmp_path / "credentials.json.enc")))

    assert recorder.credential_sync_count == 0


def test_modified_event_on_credentials_triggers_credential_sync(tmp_path: Path) -> None:
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)

    handler.dispatch(FileModifiedEvent(str(tmp_path / "credentials.json.enc")))

    assert recorder.credential_sync_count == 1
    assert recorder.permission_sync_host_ids == []


def test_modified_event_on_known_host_permissions_triggers_permission_sync(tmp_path: Path) -> None:
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    permissions_path = permissions_path_for_host(tmp_path / "mngr_latchkey", host_id)

    handler.dispatch(FileModifiedEvent(str(permissions_path)))

    assert recorder.permission_sync_host_ids == [str(host_id)]
    assert recorder.credential_sync_count == 0


def test_created_and_deleted_events_trigger_sync(tmp_path: Path) -> None:
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    credentials_path = str(tmp_path / "credentials.json.enc")

    handler.dispatch(FileCreatedEvent(credentials_path))
    handler.dispatch(FileDeletedEvent(credentials_path))

    assert recorder.credential_sync_count == 2


def test_atomic_write_rename_dest_triggers_sync(tmp_path: Path) -> None:
    """An atomic write (tmp sibling -> rename onto the real file) surfaces the
    real file as the move *dest*; the handler must match it."""
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    permissions_path = permissions_path_for_host(tmp_path / "mngr_latchkey", host_id)

    handler.dispatch(FileMovedEvent(str(permissions_path.parent / ".tmp.abc123"), str(permissions_path)))

    assert recorder.permission_sync_host_ids == [str(host_id)]


def test_unrelated_paths_and_unknown_hosts_are_ignored(tmp_path: Path) -> None:
    host_id = HostId()
    unknown_host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)
    unknown_permissions_path = permissions_path_for_host(tmp_path / "mngr_latchkey", unknown_host_id)

    handler.dispatch(FileModifiedEvent(str(tmp_path / "gateway.log")))
    handler.dispatch(FileModifiedEvent(str(unknown_permissions_path)))

    assert recorder.credential_sync_count == 0
    assert recorder.permission_sync_host_ids == []


def test_directory_events_are_ignored(tmp_path: Path) -> None:
    host_id = HostId()
    recorder = _SyncRecorder()
    handler = _build_handler(tmp_path, host_id, recorder)

    handler.dispatch(DirModifiedEvent(str(tmp_path)))

    assert recorder.credential_sync_count == 0
    assert recorder.permission_sync_host_ids == []


def _oauth_account(account: str) -> ServiceAccountCredential:
    return ServiceAccountCredential(
        account=account,
        credential_status=CredentialStatus.UNKNOWN,
        credential_type=LATCHKEY_CREDENTIAL_TYPE_OAUTH,
    )


def _zoom_server_to_server_account(account: str) -> ServiceAccountCredential:
    """Zoom's only credential kind: no refresh token, but an access token that expires."""
    return ServiceAccountCredential(
        account=account,
        credential_status=CredentialStatus.UNKNOWN,
        credential_type=LATCHKEY_CREDENTIAL_TYPE_ZOOM_SERVER_TO_SERVER,
    )


def _static_token_account(account: str) -> ServiceAccountCredential:
    return ServiceAccountCredential(
        account=account,
        credential_status=CredentialStatus.UNKNOWN,
        credential_type="authorizationBearer",
    )


@contextmanager
def _captured_log_records() -> Iterator[list[tuple[str, str]]]:
    """Collect every loguru record emitted in the block as ``(level_name, message)``."""
    captured: list[tuple[str, str]] = []
    sink_id = logger.add(lambda m: captured.append((m.record["level"].name, m.record["message"])), level=0)
    try:
        yield captured
    finally:
        logger.remove(sink_id)


@contextmanager
def _refresh_handler(
    latchkey_directory: Path,
    mngr_ctx: MngrContext,
    *,
    granted_scopes_by_host: dict[HostId, tuple[str, ...]],
    accounts_by_service: dict[str, tuple[ServiceAccountCredential, ...]],
    auth_list_error: BaseException | None = None,
) -> Iterator[tuple[LatchkeyDiscoveryHandler, FakeLatchkey]]:
    """A handler whose remote-host registry and permissions files are already populated.

    Seeds the registry the credential-refresh loop reads, which is otherwise
    only written by a live provisioning pass against a real VPS.
    """
    data_dir = plugin_data_dir(latchkey_directory)
    for host_id, scopes in granted_scopes_by_host.items():
        save_permissions(
            permissions_path_for_host(data_dir, host_id),
            LatchkeyPermissionsConfig(rules=tuple({scope: []} for scope in scopes)),
        )
    latchkey = make_full_fake_latchkey(latchkey_directory)
    latchkey.configure(accounts_by_service=accounts_by_service, auth_list_error=auth_list_error)
    tunnel_manager = SSHTunnelManager()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        try:
            handler = LatchkeyDiscoveryHandler(
                latchkey=latchkey,
                tunnel_manager=tunnel_manager,
                concurrency_group=cg,
                mngr_ctx=mngr_ctx,
            )
            for host_id in granted_scopes_by_host:
                handler._remote_host_provider_by_id[str(host_id)] = "imbue_cloud"
            yield handler, latchkey
        finally:
            tunnel_manager.cleanup()


def test_refresh_probes_only_the_renewable_services_a_remote_host_is_granted(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """The renewal probe is the non-offline one, and only renewable credentials get it.

    Zoom is here because it is the one renewable kind that is not OAuth: it
    holds no refresh token, minting a fresh access token from the app's client
    id and secret instead, so narrowing on ``oauth`` alone would leave every
    Zoom-granted workspace unrenewed.
    """
    host_id = HostId.generate()
    with _refresh_handler(
        tmp_path,
        temp_mngr_ctx,
        granted_scopes_by_host={host_id: ("google-gmail-api", "github-rest-api", "zoom-api")},
        accounts_by_service={
            "google-gmail": (_oauth_account("someone@example.com"),),
            "github": (_static_token_account("someone"),),
            "zoom": (_zoom_server_to_server_account("someone@example.com"),),
        },
    ) as (handler, latchkey):
        handler._refresh_remote_credentials_once()

    # GitHub's static token never expires, so probing it would be a third-party
    # round-trip that could not change any outcome.
    assert latchkey.services_info_calls == (("google-gmail", False), ("zoom", False))
    # The narrowing read is one offline ``auth list`` for the whole pass: a
    # non-offline one would validate every stored account against its third
    # party, which is exactly the traffic the narrowing exists to avoid.
    assert latchkey.auth_list_calls == (True,)


@pytest.mark.parametrize(
    "accounts_by_service",
    [
        # Something else is connected, so the store reads fine -- google-gmail
        # simply has no account in it.
        pytest.param({"slack": (_static_token_account("someone"),)}, id="another-service-connected"),
        # Nothing is stored at all, which is also what an ``auth list`` that
        # could not answer degrades to -- and it has already named that cause
        # itself, so this pass must not report it a second time.
        pytest.param({}, id="empty-store"),
    ],
)
def test_a_grant_whose_account_was_never_stored_is_quietly_skipped(
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    accounts_by_service: dict[str, tuple[ServiceAccountCredential, ...]],
) -> None:
    """A grant outliving (or preceding) its credentials is supported, so it must not warn.

    This pass runs every five minutes for the supervisor's lifetime, so treating
    a legitimate state as a fault would emit hundreds of warnings a day about
    something the user has no reason to act on.
    """
    host_id = HostId.generate()
    with _captured_log_records() as captured:
        with _refresh_handler(
            tmp_path,
            temp_mngr_ctx,
            granted_scopes_by_host={host_id: ("google-gmail-api",)},
            accounts_by_service=accounts_by_service,
        ) as (handler, latchkey):
            handler._refresh_remote_credentials_once()

    assert latchkey.services_info_calls == ()
    assert [level for level, _message in captured if level in ("WARNING", "ERROR")] == []


def test_a_latchkey_failure_costs_only_the_pass(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """An expected latchkey failure must end the pass, not the renewal loop."""
    host_id = HostId.generate()
    with _captured_log_records() as captured:
        with _refresh_handler(
            tmp_path,
            temp_mngr_ctx,
            granted_scopes_by_host={host_id: ("google-gmail-api",)},
            accounts_by_service={"google-gmail": (_oauth_account("someone@example.com"),)},
            # What ``Latchkey._load_encryption_key`` raises when a stray ``chmod``
            # leaves the key file readable by other local users.
            auth_list_error=LatchkeyError("encryption key file is group-readable"),
        ) as (handler, latchkey):
            handler._refresh_remote_credentials_once()

    assert latchkey.services_info_calls == ()
    assert any(level == "ERROR" and "Could not renew" in message for level, message in captured)


def test_elapsed_time_ignores_whichever_clock_stalled() -> None:
    """Each clock has a failure mode the other does not, so the larger reading wins."""
    # A machine that slept: macOS froze the monotonic clock, the wall clock moved on.
    assert _seconds_since(time.monotonic(), time.time() - 3600.0) >= 3600.0
    # A wall clock stepped backwards by an NTP correction: monotonic still holds the truth.
    assert _seconds_since(time.monotonic() - 3600.0, time.time() + 3600.0) >= 3600.0


def test_first_renewal_pass_waits_for_a_remote_host_rather_than_for_the_interval() -> None:
    """A restarted supervisor renews at once: nothing renewed while it was down."""
    # The loop starts before discovery registers any host, so an empty registry
    # must leave the interval unstarted instead of consuming it.
    assert not _is_renewal_pass_due(False, None)
    assert _is_renewal_pass_due(True, None)
    assert not _is_renewal_pass_due(True, (time.monotonic(), time.time()))


def test_refresh_skips_a_connected_service_no_remote_host_is_granted(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Credentials that never reach a VPS are the desktop's own business."""
    host_id = HostId.generate()
    with _refresh_handler(
        tmp_path,
        temp_mngr_ctx,
        granted_scopes_by_host={host_id: ("slack-api",)},
        accounts_by_service={
            "google-gmail": (_oauth_account("someone@example.com"),),
            "slack": (_static_token_account("someone"),),
        },
    ) as (handler, latchkey):
        handler._refresh_remote_credentials_once()

    assert latchkey.services_info_calls == ()


def test_one_hosts_corrupt_permissions_file_does_not_block_the_other_hosts(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Renewal degrades per host, the way the sync path already does."""
    broken_host = HostId.generate()
    healthy_host = HostId.generate()
    with _captured_log_records() as captured:
        with _refresh_handler(
            tmp_path,
            temp_mngr_ctx,
            granted_scopes_by_host={broken_host: ("slack-api",), healthy_host: ("google-gmail-api",)},
            accounts_by_service={
                "google-gmail": (_oauth_account("someone@example.com"),),
                "slack": (_oauth_account("someone"),),
            },
        ) as (handler, latchkey):
            permissions_path_for_host(plugin_data_dir(tmp_path), broken_host).write_text("{ not json")
            handler._refresh_remote_credentials_once()

    # Slack is granted only by the broken host, so it drops out with that host.
    assert latchkey.services_info_calls == (("google-gmail", False),)
    assert any(
        level == "ERROR" and "Skipping host" in message and str(broken_host) in message for level, message in captured
    )


def test_refresh_probes_nothing_when_there_are_no_remote_hosts(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """A user with only local workspaces pays no third-party traffic for this."""
    with _refresh_handler(
        tmp_path,
        temp_mngr_ctx,
        granted_scopes_by_host={},
        accounts_by_service={"google-gmail": (_oauth_account("someone@example.com"),)},
    ) as (handler, latchkey):
        handler._refresh_remote_credentials_once()

    assert latchkey.services_info_calls == ()


def test_refresh_covers_every_remote_host_and_probes_each_service_once(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Two hosts granting overlapping services yield one probe per service, not per host."""
    first_host = HostId.generate()
    second_host = HostId.generate()
    with _refresh_handler(
        tmp_path,
        temp_mngr_ctx,
        granted_scopes_by_host={
            first_host: ("google-gmail-api",),
            second_host: ("google-gmail-api", "dropbox-api"),
        },
        accounts_by_service={
            "google-gmail": (_oauth_account("someone@example.com"),),
            "dropbox": (_oauth_account("someone@example.com"),),
        },
    ) as (handler, latchkey):
        handler._refresh_remote_credentials_once()

    assert sorted(latchkey.services_info_calls) == [("dropbox", False), ("google-gmail", False)]
