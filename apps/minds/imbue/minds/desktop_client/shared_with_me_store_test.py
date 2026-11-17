import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import SHARED_TEST_HOST_ID
from imbue.minds.desktop_client.conftest import SHARED_TEST_WORKSPACE_ID
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_shared_share
from imbue.minds.desktop_client.conftest import make_shared_with_me_store_for_test
from imbue.minds.desktop_client.conftest import matches_of_shares
from imbue.minds.desktop_client.conftest import seed_shared_with_me_listing
from imbue.minds.desktop_client.imbue_cloud_cli import SharedWithMeShareCliInfo
from imbue.minds.desktop_client.share_targets import WHOLE_MACHINE_SERVICE
from imbue.minds.desktop_client.shared_with_me_store import SHARED_WITH_ME_DOCUMENT_KIND
from imbue.minds.desktop_client.shared_with_me_store import SHARED_WITH_ME_STALE_AFTER
from imbue.minds.desktop_client.shared_with_me_store import SharedAccessVerdict
from imbue.minds.desktop_client.shared_with_me_store import SharedEntryKind
from imbue.minds.desktop_client.shared_with_me_store import SharedWithMeCache
from imbue.minds.desktop_client.shared_with_me_store import SharedWithMeEntry
from imbue.minds.desktop_client.shared_with_me_store import SharedWithMeStore
from imbue.minds.desktop_client.shared_with_me_store import entry_from_metadata
from imbue.minds.desktop_client.shared_with_me_store import is_refresh_needed
from imbue.minds.desktop_client.shared_with_me_store import manual_link_doc_id
from imbue.minds.desktop_client.shared_with_me_store import merge_shared_rows
from imbue.minds.desktop_client.shared_with_me_store import normalize_manual_link
from imbue.minds.desktop_client.shared_with_me_store import shared_workspace_ui_entry
from imbue.minds.errors import SharedLinkRowMissingError

_USER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_EMAIL = "bob@acme.example"
_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def _cache(*shares: SharedWithMeShareCliInfo, version: int = 1, fetched_at: datetime = _NOW) -> SharedWithMeCache:
    return SharedWithMeCache(
        matches=matches_of_shares(shares, version=version), fetched_at=fetched_at, shares=tuple(shares)
    )


def _store_with_listing(
    data_dir: Path, *shares: SharedWithMeShareCliInfo
) -> tuple[FakeImbueCloudCli, SharedWithMeStore]:
    """A store over ``data_dir`` whose fake connector lists ``shares`` (cursor ``c1``) for the test account."""
    cli = make_fake_imbue_cloud_cli()
    seed_shared_with_me_listing(cli, _EMAIL, *shares)
    return cli, make_shared_with_me_store_for_test(data_dir, cli)


# Pure helpers


def test_normalize_manual_link_keeps_only_an_https_origin() -> None:
    assert normalize_manual_link(" https://Shell-1.abc.def.us1.shares.example/some/path?q=1 ") == (
        "https://shell-1.abc.def.us1.shares.example/"
    )
    assert normalize_manual_link("https://host.example:8443/x") == "https://host.example:8443/"
    assert normalize_manual_link("http://host.example/") is None
    assert normalize_manual_link("https://user:pw@host.example/") is None
    assert normalize_manual_link("https://host.example:abc/") is None
    assert normalize_manual_link("https://host.example:99999/") is None
    assert normalize_manual_link("https://[::1]/") is None
    assert normalize_manual_link("not a link") is None
    assert normalize_manual_link("") is None


def test_manual_link_doc_id_is_stable_and_document_id_shaped() -> None:
    first = manual_link_doc_id("https://a.example/")
    assert first == manual_link_doc_id("https://a.example/")
    assert first != manual_link_doc_id("https://b.example/")
    # A bare hex digest: the SPA routes it as `shared-<key>`, whose id segment admits hex alone.
    assert re.fullmatch(r"[a-f0-9]{40}", first)


def test_entry_from_metadata_round_trips_and_rejects_garbage() -> None:
    entry = SharedWithMeEntry(
        kind=SharedEntryKind.MANUAL,
        link="https://x.example/",
        last_verdict=SharedAccessVerdict.REFUSED,
        last_verdict_at=_NOW,
        label="Pasted",
    )
    assert entry_from_metadata(entry.as_metadata()) == entry
    assert entry_from_metadata({"kind": "listed", "link": "https://x.example/"}) is not None
    assert entry_from_metadata({"kind": "nonsense"}) is None
    assert entry_from_metadata({"last_verdict": "maybe"}) is None


def test_is_refresh_needed_on_absence_match_set_change_and_staleness_only() -> None:
    share = make_shared_share(SHARED_TEST_HOST_ID)
    cache = _cache(share, version=1, fetched_at=_NOW)
    same = matches_of_shares([share], version=1)
    moved = matches_of_shares([share], version=2)
    assert is_refresh_needed(None, same, _NOW) is True
    assert is_refresh_needed(cache, moved, _NOW) is True
    assert is_refresh_needed(cache, (), _NOW) is True
    assert is_refresh_needed(cache, same, _NOW) is False
    assert is_refresh_needed(cache, None, _NOW) is False
    assert is_refresh_needed(cache, same, _NOW + SHARED_WITH_ME_STALE_AFTER) is True


def test_merge_shared_rows_joins_verdicts_and_folds_pasted_links_under_listed_domains() -> None:
    share = make_shared_share(
        SHARED_TEST_HOST_ID, workspace_id=SHARED_TEST_WORKSPACE_ID, app_labels={"notes": "notes-7f2k"}
    )
    entries = {
        SHARED_TEST_WORKSPACE_ID: SharedWithMeEntry(
            kind=SharedEntryKind.LISTED,
            link=share.targets[0].link or "",
            last_verdict=SharedAccessVerdict.ADMITTED,
            last_verdict_at=_NOW,
            label="",
        ),
        manual_link_doc_id(f"https://notes-7f2k.{share.workspace_domain}/"): SharedWithMeEntry(
            kind=SharedEntryKind.MANUAL,
            link=f"https://notes-7f2k.{share.workspace_domain}/",
            last_verdict=None,
            last_verdict_at=None,
            label="dup",
        ),
        manual_link_doc_id("https://shell-9.other.us1.shares.example/"): SharedWithMeEntry(
            kind=SharedEntryKind.MANUAL,
            link="https://shell-9.other.us1.shares.example/",
            last_verdict=SharedAccessVerdict.OFFLINE,
            last_verdict_at=_NOW,
            label="Elsewhere",
        ),
    }

    rows = merge_shared_rows(_cache(share), entries)

    assert [(row.share_key, row.is_manual, row.verdict) for row in rows] == [
        (SHARED_TEST_WORKSPACE_ID, False, SharedAccessVerdict.ADMITTED),
        (manual_link_doc_id("https://shell-9.other.us1.shares.example/"), True, SharedAccessVerdict.OFFLINE),
    ]
    assert rows[0].shell_link == share.targets[0].link
    assert rows[0].link_for_app("notes") == f"https://notes-7f2k.{share.workspace_domain}/"
    assert rows[0].link_for_app("missing") is None
    assert rows[1].workspace_domain == "shell-9.other.us1.shares.example"
    assert rows[1].workspace_name == "Elsewhere"
    assert rows[1].targets[0].app == WHOLE_MACHINE_SERVICE


def test_shared_workspace_ui_entry_lists_the_row_under_the_recipient_and_not_owned() -> None:
    share = make_shared_share(
        SHARED_TEST_HOST_ID,
        workspace_id=SHARED_TEST_WORKSPACE_ID,
        app_labels={"notes": "notes-7f2k", "crm": "crm-2b8x"},
        granted_apps={"notes"},
    )
    (row,) = merge_shared_rows(_cache(share), {})

    entry = shared_workspace_ui_entry(row, _USER_ID, _EMAIL)

    assert entry.id == SHARED_TEST_WORKSPACE_ID
    assert entry.name == "Alpha"
    assert entry.is_owned is False
    assert entry.account_user_id == _USER_ID
    assert entry.account == _EMAIL
    assert entry.share is not None
    assert entry.share.owner.email == "owner@acme.example"
    assert entry.share.status == "invited"
    assert entry.share.targets[0].link == share.targets[0].link
    assert [(target.app, target.is_granted_as_app) for target in entry.share.targets] == [
        (WHOLE_MACHINE_SERVICE, False),
        ("notes", True),
        ("crm", False),
    ]
    assert entry.share.verdict is None


# The store


def test_refresh_fetches_through_the_cli_and_persists_the_cache_across_instances(tmp_path: Path) -> None:
    cli, store = _store_with_listing(tmp_path, make_shared_share(SHARED_TEST_HOST_ID))
    changes: list[bool] = []
    store.on_change = lambda: changes.append(True)

    assert store.refresh(_USER_ID, _EMAIL, _NOW) is True

    assert changes == [True]
    reopened = make_shared_with_me_store_for_test(tmp_path, cli)
    cache = reopened.read_cache(_USER_ID)
    assert cache is not None and [match.version for match in cache.matches] == [1]
    assert [row.host_id for row in reopened.list_rows(_USER_ID)] == [SHARED_TEST_HOST_ID]
    assert reopened.listed_share_coordinates() == frozenset({SHARED_TEST_HOST_ID})


def test_refresh_if_needed_skips_an_unchanged_match_set_and_keeps_the_cache_on_a_failed_fetch(
    tmp_path: Path,
) -> None:
    share = make_shared_share(SHARED_TEST_HOST_ID)
    cli, store = _store_with_listing(tmp_path, share)
    same = matches_of_shares([share], version=1)
    moved = matches_of_shares([share], version=2)

    assert store.refresh_if_needed(_USER_ID, _EMAIL, same, False, _NOW) is True
    assert store.refresh_if_needed(_USER_ID, _EMAIL, same, False, _NOW) is False
    assert store.refresh_if_needed(_USER_ID, _EMAIL, None, False, _NOW) is False
    assert store.refresh_if_needed(_USER_ID, _EMAIL, same, True, _NOW) is True
    assert cli.shared_with_me_fetch_count == 2

    cli.is_shared_with_me_failing = True
    assert store.refresh_if_needed(_USER_ID, _EMAIL, moved, False, _NOW) is False
    cache = store.read_cache(_USER_ID)
    assert cache is not None and cache.matches == same
    assert store.refresh_if_needed(_USER_ID, _EMAIL, same, False, _NOW + timedelta(minutes=11)) is False


def test_a_fetch_that_started_before_a_newer_one_landed_does_not_undo_it(tmp_path: Path) -> None:
    cli, store = _store_with_listing(tmp_path, make_shared_share(SHARED_TEST_HOST_ID, status="invited"))
    assert store.refresh(_USER_ID, _EMAIL, _NOW) is True
    seed_shared_with_me_listing(cli, _EMAIL, make_shared_share(SHARED_TEST_HOST_ID, status="accepted"), version=2)
    assert store.refresh(_USER_ID, _EMAIL, _NOW + timedelta(seconds=2)) is True

    # The listing has moved back on the fake: a fetch stamped before the newer one keeps the newer rows.
    seed_shared_with_me_listing(cli, _EMAIL, make_shared_share(SHARED_TEST_HOST_ID, status="invited"))
    assert store.refresh(_USER_ID, _EMAIL, _NOW + timedelta(seconds=1)) is True

    cache = store.read_cache(_USER_ID)
    assert cache is not None and cache.matches[0].version == 2 and cache.shares[0].status == "accepted"


def test_a_connector_without_the_listing_is_cached_as_an_empty_listing(tmp_path: Path) -> None:
    # Spec section 7: against an old connector the feature is absent, not
    # failing, so the pass stops asking until the staleness window.
    cli, store = _store_with_listing(tmp_path, make_shared_share(SHARED_TEST_HOST_ID))
    cli.is_shared_with_me_unavailable = True

    assert store.refresh_if_needed(_USER_ID, _EMAIL, None, False, _NOW) is True
    assert store.list_rows(_USER_ID) == []
    assert store.refresh_if_needed(_USER_ID, _EMAIL, None, False, _NOW + timedelta(minutes=5)) is False
    assert store.refresh_if_needed(_USER_ID, _EMAIL, None, False, _NOW + SHARED_WITH_ME_STALE_AFTER) is True
    assert cli.shared_with_me_fetch_count == 2


def test_verdicts_and_pasted_links_live_in_the_account_document_and_sync_through_the_cli(tmp_path: Path) -> None:
    cli, store = _store_with_listing(
        tmp_path, make_shared_share(SHARED_TEST_HOST_ID, workspace_id=SHARED_TEST_WORKSPACE_ID)
    )
    store.refresh(_USER_ID, _EMAIL, _NOW)

    changes: list[bool] = []
    store.on_change = lambda: changes.append(True)
    store.record_verdict(_USER_ID, SHARED_TEST_WORKSPACE_ID, SharedAccessVerdict.REFUSED, _NOW)
    # The same verdict again (every navigation inside the frame reports one) rewrites nothing.
    store.record_verdict(_USER_ID, SHARED_TEST_WORKSPACE_ID, SharedAccessVerdict.REFUSED, _NOW + timedelta(minutes=1))
    row = store.find_row(_USER_ID, SHARED_TEST_WORKSPACE_ID)
    assert row is not None and row.verdict is SharedAccessVerdict.REFUSED and row.verdict_at == _NOW
    assert changes == [True]
    store.record_verdict(_USER_ID, SHARED_TEST_WORKSPACE_ID, SharedAccessVerdict.ADMITTED, _NOW + timedelta(minutes=2))
    row = store.find_row(_USER_ID, SHARED_TEST_WORKSPACE_ID)
    assert row is not None and row.verdict is SharedAccessVerdict.ADMITTED
    assert changes == [True, True]

    store.clear_verdict(_USER_ID, SHARED_TEST_WORKSPACE_ID)
    row = store.find_row(_USER_ID, SHARED_TEST_WORKSPACE_ID)
    assert row is not None and row.verdict is None
    with pytest.raises(SharedLinkRowMissingError, match="agent-unknown"):
        store.record_verdict(_USER_ID, "agent-unknown", SharedAccessVerdict.REFUSED, _NOW)

    manual = store.add_manual_link(_USER_ID, "https://shell-9.other.us1.shares.example/", "Elsewhere")
    assert manual.is_manual and manual.workspace_name == "Elsewhere"
    assert [listed.workspace_domain for listed in store.list_rows(_USER_ID)] == [
        row.workspace_domain,
        "shell-9.other.us1.shares.example",
    ]

    # The documents reach the fake connector on a push and come back on a pull elsewhere.
    store.documents.push_dirty(_USER_ID, _EMAIL)
    pushed = cli.sync_documents_by_email[_EMAIL][SHARED_WITH_ME_DOCUMENT_KIND]
    assert set(pushed) == {SHARED_TEST_WORKSPACE_ID, manual.share_key}
    other_device = make_shared_with_me_store_for_test(tmp_path / "other", cli)
    other_device.documents.pull(_USER_ID, _EMAIL)
    other_device.refresh(_USER_ID, _EMAIL, _NOW)
    assert [candidate.share_key for candidate in other_device.list_rows(_USER_ID)] == [
        SHARED_TEST_WORKSPACE_ID,
        manual.share_key,
    ]

    assert store.remove_manual_link(_USER_ID, manual.share_key) is True
    assert store.remove_manual_link(_USER_ID, manual.share_key) is False
    assert [candidate.share_key for candidate in store.list_rows(_USER_ID)] == [SHARED_TEST_WORKSPACE_ID]


def test_a_pasted_link_under_a_listed_domain_answers_that_share_row(tmp_path: Path) -> None:
    share = make_shared_share(SHARED_TEST_HOST_ID, workspace_id=SHARED_TEST_WORKSPACE_ID)
    _cli, store = _store_with_listing(tmp_path, share)
    store.refresh(_USER_ID, _EMAIL, _NOW)

    row = store.add_manual_link(_USER_ID, f"https://whatever.{share.workspace_domain}/", "")

    assert row.share_key == SHARED_TEST_WORKSPACE_ID and row.is_manual is False
    # Nothing is recorded for a link the listing already covers.
    assert store.documents.read_entries(_USER_ID) == {}


def test_a_pasted_link_names_its_recipient_before_any_listing_was_cached(tmp_path: Path) -> None:
    """An account whose every fetch failed has no cache file, yet its pasted links' windows are its own."""
    store = make_shared_with_me_store_for_test(tmp_path, make_fake_imbue_cloud_cli())

    pasted = store.add_manual_link(_USER_ID, "https://shell-9.other.us1.shares.example/", "")

    assert store.list_cached_user_ids() == []
    assert store.recipient_account_id_of(pasted.share_key) == _USER_ID
    assert store.recipient_account_id_of(f"shared-{pasted.share_key}") == _USER_ID
    assert store.recipient_account_id_of("agent-unknown") is None


def test_forget_account_drops_the_cache_file(tmp_path: Path) -> None:
    _cli, store = _store_with_listing(tmp_path, make_shared_share(SHARED_TEST_HOST_ID))
    store.refresh(_USER_ID, _EMAIL, _NOW)

    store.forget_account(_USER_ID)

    assert store.read_cache(_USER_ID) is None
    assert store.list_cached_user_ids() == []
