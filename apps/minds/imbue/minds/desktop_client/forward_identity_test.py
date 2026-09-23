import json
from pathlib import Path

from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.forward_identity import ForwardHeadersFile
from imbue.minds.desktop_client.forward_identity import ForwardIdentityPublisher
from imbue.minds.desktop_client.forward_identity import IDENTITY_HEADER
from imbue.minds.desktop_client.forward_identity import OwnerIdentity
from imbue.minds.desktop_client.forward_identity import remove_legacy_forward_identity_file
from imbue.minds.desktop_client.forward_identity import render_forward_headers
from imbue.minds.desktop_client.forward_identity import render_identity_header
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.request_headers import RequestHeadersFileReader

_OWNER_ID = "33333333-3333-3333-3333-333333333333"
_OTHER_ID = "44444444-4444-4444-4444-444444444444"
_AGENT_A = str(AgentId.generate())
_AGENT_B = str(AgentId.generate())
_AGENT_C = str(AgentId.generate())


def test_identity_header_renders_exactly_the_two_forms_the_workspace_shell_parses() -> None:
    # These literals are the contract with the workspace shell's presence
    # store: key order and compactness included, so a byte comparison there
    # keeps working.
    assert IDENTITY_HEADER == "X-Imbue-Identity"
    assert render_identity_header(None) == '{"owner":true}'
    assert render_identity_header(OwnerIdentity(user_id="u", email="e")) == '{"owner":true,"user_id":"u","email":"e"}'


def test_forward_headers_carry_the_owner_flag_for_every_agent_and_the_account_for_shared_ones() -> None:
    rendered = render_forward_headers({_AGENT_A: OwnerIdentity(user_id=_OWNER_ID, email="owner@example.com")})

    assert rendered.headers_by_agent_key == {
        "*": {IDENTITY_HEADER: '{"owner":true}'},
        _AGENT_A: {IDENTITY_HEADER: f'{{"owner":true,"user_id":"{_OWNER_ID}","email":"owner@example.com"}}'},
    }


def test_headers_file_writes_what_the_proxys_reader_parses_and_skips_unchanged_rewrites(tmp_path: Path) -> None:
    headers_file = ForwardHeadersFile(path=tmp_path / "nested" / "forward_headers.json")
    identity = OwnerIdentity(user_id=_OWNER_ID, email="owner@example.com")

    headers_file.write({_AGENT_A: identity})

    reader = RequestHeadersFileReader(path=headers_file.path)
    assert json.loads(reader.headers_for_agent(_AGENT_A).values_by_name[IDENTITY_HEADER]) == {
        "owner": True,
        "user_id": _OWNER_ID,
        "email": "owner@example.com",
    }
    assert json.loads(reader.headers_for_agent(_AGENT_B).values_by_name[IDENTITY_HEADER]) == {"owner": True}
    assert reader.headers_for_agent(_AGENT_B).names_to_strip == frozenset({"x-imbue-identity"})

    # An identical rendering leaves the file untouched (the proxy re-parses
    # on any mtime change, so needless rewrites would cost it a parse each).
    before = headers_file.path.stat()
    headers_file.write({_AGENT_A: identity})
    assert headers_file.path.stat().st_mtime_ns == before.st_mtime_ns


def _publisher(tmp_path: Path) -> tuple[ForwardIdentityPublisher, ForwardHeadersFile, list[str], FakeImbueCloudCli]:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_OWNER_ID, email="owner@example.com")
    cli.add_account(user_id=_OTHER_ID, email="other@example.com")
    store = make_session_store_for_test(tmp_path, cli=cli)
    headers_file = ForwardHeadersFile(path=tmp_path / "forward_headers.json")
    sync_requests: list[str] = []
    publisher = ForwardIdentityPublisher(
        session_store=store,
        headers_file=headers_file,
        on_shared_workspaces_changed=lambda: sync_requests.append("kick"),
    )
    return publisher, headers_file, sync_requests, cli


def _identity_by_agent_id(headers_file: ForwardHeadersFile) -> dict[str, dict[str, object]]:
    document = json.loads(headers_file.path.read_text())
    return {key: json.loads(entry[IDENTITY_HEADER]) for key, entry in document.items() if key != "*"}


def test_rebuild_writes_only_the_default_entry_when_nothing_is_shared(tmp_path: Path) -> None:
    publisher, headers_file, _, _ = _publisher(tmp_path)

    publisher.rebuild()

    assert json.loads(headers_file.path.read_text()) == {"*": {IDENTITY_HEADER: '{"owner":true}'}}


def test_sync_results_map_each_shared_workspace_to_its_owning_accounts_session(tmp_path: Path) -> None:
    publisher, headers_file, sync_requests, _ = _publisher(tmp_path)

    publisher.apply_sync_results({_OWNER_ID: [_AGENT_A, _AGENT_B], _OTHER_ID: [_AGENT_C]})

    assert _identity_by_agent_id(headers_file) == {
        _AGENT_A: {"owner": True, "user_id": _OWNER_ID, "email": "owner@example.com"},
        _AGENT_B: {"owner": True, "user_id": _OWNER_ID, "email": "owner@example.com"},
        _AGENT_C: {"owner": True, "user_id": _OTHER_ID, "email": "other@example.com"},
    }
    # Applying a pass never asks for another pass.
    assert sync_requests == []

    # An account absent from the results (its pull failed) keeps its previous
    # set; a listed account's set is replaced wholesale.
    publisher.apply_sync_results({_OWNER_ID: [_AGENT_B]})
    assert sorted(_identity_by_agent_id(headers_file)) == sorted([_AGENT_B, _AGENT_C])


def test_local_share_changes_edit_the_file_immediately_and_a_share_requests_a_sync(tmp_path: Path) -> None:
    publisher, headers_file, sync_requests, _ = _publisher(tmp_path)

    publisher.mark_shared(_AGENT_A, _OWNER_ID)
    assert _identity_by_agent_id(headers_file) == {
        _AGENT_A: {"owner": True, "user_id": _OWNER_ID, "email": "owner@example.com"}
    }
    assert sync_requests == ["kick"]

    publisher.mark_unshared(_AGENT_A)
    assert _identity_by_agent_id(headers_file) == {}
    # Unsharing leaves the sync request to the caller, once the connector share is gone.
    assert sync_requests == ["kick"]
    publisher.request_sync()
    assert sync_requests == ["kick", "kick"]


def test_rebuild_drops_the_workspaces_of_an_account_no_longer_signed_in(tmp_path: Path) -> None:
    publisher, headers_file, _, cli = _publisher(tmp_path)
    publisher.apply_sync_results({_OWNER_ID: [_AGENT_A], _OTHER_ID: [_AGENT_C]})

    cli.remove_account(_OTHER_ID)
    publisher.session_store.invalidate_identity_cache()
    publisher.rebuild()

    assert list(_identity_by_agent_id(headers_file)) == [_AGENT_A]


def test_rebuild_leaves_the_file_alone_while_the_account_listing_is_unavailable(tmp_path: Path) -> None:
    publisher, headers_file, _, cli = _publisher(tmp_path)
    publisher.apply_sync_results({_OWNER_ID: [_AGENT_A]})

    cli.is_auth_list_failing = True
    publisher.session_store.invalidate_identity_cache()
    publisher.rebuild()

    # A transient listing failure must not strip the owner's account from
    # every shared workspace's requests.
    assert list(_identity_by_agent_id(headers_file)) == [_AGENT_A]


def test_remove_legacy_forward_identity_file_is_a_no_op_without_one(tmp_path: Path) -> None:
    legacy = tmp_path / "forward_identity.json"
    legacy.write_text("{}")

    remove_legacy_forward_identity_file(tmp_path)
    remove_legacy_forward_identity_file(tmp_path)

    assert not legacy.exists()
