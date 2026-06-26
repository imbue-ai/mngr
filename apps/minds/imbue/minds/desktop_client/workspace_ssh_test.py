from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from imbue.minds.desktop_client.workspace_ssh import SshGrantError
from imbue.minds.desktop_client.workspace_ssh import build_authorized_keys_line
from imbue.minds.desktop_client.workspace_ssh import compose_pruned_authorized_keys
from imbue.minds.desktop_client.workspace_ssh import prune_expired_grant_lines

_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEYMATERIAL"


def test_build_authorized_keys_line_tags_requester_and_expiry() -> None:
    expires_at = _NOW + timedelta(hours=24)
    line = build_authorized_keys_line(public_key=_KEY, requester_workspace_id="agent-abc", expires_at=expires_at)

    assert line.startswith("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEYMATERIAL ")
    assert "minds-ssh-grant" in line
    assert "requester=agent-abc" in line
    assert f"expires={expires_at.isoformat()}" in line


def test_build_authorized_keys_line_drops_caller_comment() -> None:
    line = build_authorized_keys_line(
        public_key=f"{_KEY} caller-comment@host", requester_workspace_id="agent-abc", expires_at=_NOW
    )

    assert "caller-comment@host" not in line
    assert line.count("minds-ssh-grant") == 1


def test_build_authorized_keys_line_rejects_multiline_key() -> None:
    with pytest.raises(SshGrantError):
        build_authorized_keys_line(public_key=f"{_KEY}\nssh-rsa INJECTED", requester_workspace_id="a", expires_at=_NOW)


def test_build_authorized_keys_line_rejects_non_key() -> None:
    with pytest.raises(SshGrantError):
        build_authorized_keys_line(public_key="not a key", requester_workspace_id="a", expires_at=_NOW)


def test_build_authorized_keys_line_rejects_empty() -> None:
    with pytest.raises(SshGrantError):
        build_authorized_keys_line(public_key="   ", requester_workspace_id="a", expires_at=_NOW)


def test_build_authorized_keys_line_rejects_requester_with_newline() -> None:
    # A newline in the requester id would inject a second authorized_keys line,
    # exactly the injection the public-key validation guards against.
    with pytest.raises(SshGrantError):
        build_authorized_keys_line(
            public_key=_KEY,
            requester_workspace_id="agent-abc\nssh-rsa INJECTED",
            expires_at=_NOW,
        )


def test_prune_expired_grant_lines_drops_expired_minds_keys() -> None:
    expired = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="old", expires_at=_NOW - timedelta(hours=1)
    )
    live = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="new", expires_at=_NOW + timedelta(hours=1)
    )
    content = f"{expired}\n{live}\n"

    pruned = prune_expired_grant_lines(content, now=_NOW)

    assert "requester=old" not in pruned
    assert "requester=new" in pruned


def test_prune_expired_grant_lines_preserves_user_managed_keys() -> None:
    user_key = "ssh-rsa AAAAUSERKEY user@laptop"
    expired = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="old", expires_at=_NOW - timedelta(hours=1)
    )
    content = f"{user_key}\n{expired}\n"

    pruned = prune_expired_grant_lines(content, now=_NOW)

    assert user_key in pruned
    assert "requester=old" not in pruned


def test_prune_expired_grant_lines_empty_content_stays_empty() -> None:
    assert prune_expired_grant_lines("", now=_NOW) == ""


def test_prune_expired_grant_lines_drops_grant_with_corrupt_expiry() -> None:
    # A minds-owned grant whose ``expires=`` marker is unparseable is treated as
    # expired (the epoch sentinel) and dropped. ``now`` is timezone-aware, so the
    # sentinel must be aware too, or the comparison would raise TypeError.
    live = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="new", expires_at=_NOW + timedelta(hours=1)
    )
    corrupt = f"{_KEY} minds-ssh-grant requester=old expires=not-a-timestamp"
    content = f"{corrupt}\n{live}\n"

    pruned = prune_expired_grant_lines(content, now=_NOW)

    assert "requester=old" not in pruned
    assert "requester=new" in pruned


def test_compose_pruned_authorized_keys_appends_new_grant_to_empty_file() -> None:
    new_line = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="caller", expires_at=_NOW + timedelta(hours=24)
    )

    composed = compose_pruned_authorized_keys("", new_line, requester_workspace_id="caller", now=_NOW)

    assert composed == f"{new_line}\n"


def test_compose_pruned_authorized_keys_prunes_expired_and_keeps_user_and_live_grants() -> None:
    user_key = "ssh-rsa AAAAUSERKEY user@laptop"
    expired = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="stale", expires_at=_NOW - timedelta(hours=1)
    )
    live = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="still-good", expires_at=_NOW + timedelta(hours=1)
    )
    new_line = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="caller", expires_at=_NOW + timedelta(hours=24)
    )
    existing = f"{user_key}\n{expired}\n{live}\n"

    composed = compose_pruned_authorized_keys(existing, new_line, requester_workspace_id="caller", now=_NOW)

    # The user's hand-managed key and the still-valid grant (a different
    # requester) survive; the expired grant is dropped; the new grant is appended
    # last; the body ends in a newline.
    assert user_key in composed
    assert "requester=still-good" in composed
    assert "requester=stale" not in composed
    assert composed.endswith(f"{new_line}\n")
    assert "requester=caller" in composed


def test_compose_pruned_authorized_keys_does_not_duplicate_newlines() -> None:
    user_key = "ssh-rsa AAAAUSERKEY user@laptop"
    new_line = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="caller", expires_at=_NOW + timedelta(hours=24)
    )

    composed = compose_pruned_authorized_keys(f"{user_key}\n", new_line, requester_workspace_id="caller", now=_NOW)

    assert composed == f"{user_key}\n{new_line}\n"
    assert "\n\n" not in composed


def test_compose_pruned_authorized_keys_replaces_prior_grant_for_same_requester() -> None:
    user_key = "ssh-rsa AAAAUSERKEY user@laptop"
    prior_same_requester = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="caller", expires_at=_NOW + timedelta(hours=1)
    )
    other_requester = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="someone-else", expires_at=_NOW + timedelta(hours=1)
    )
    new_line = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="caller", expires_at=_NOW + timedelta(hours=24)
    )
    existing = f"{user_key}\n{prior_same_requester}\n{other_requester}\n"

    composed = compose_pruned_authorized_keys(existing, new_line, requester_workspace_id="caller", now=_NOW)

    # A re-request refreshes rather than stacks: the caller's prior still-valid
    # grant is replaced by the new one (exactly one caller grant remains), while
    # the user key and the unrelated requester's grant are untouched.
    assert composed.count("requester=caller") == 1
    assert composed.endswith(f"{new_line}\n")
    assert user_key in composed
    assert "requester=someone-else" in composed


def test_prune_expired_grant_lines_drops_grant_with_naive_expiry() -> None:
    # A parseable but timezone-naive ``expires=`` is treated as expired (the
    # aware epoch sentinel) rather than compared directly, so the prune does
    # not raise TypeError on naive-vs-aware datetimes and the grant is dropped
    # even though its naive timestamp is in the far future.
    live = build_authorized_keys_line(
        public_key=_KEY, requester_workspace_id="new", expires_at=_NOW + timedelta(hours=1)
    )
    naive = f"{_KEY} minds-ssh-grant requester=old expires=2099-01-01T00:00:00"
    content = f"{naive}\n{live}\n"

    pruned = prune_expired_grant_lines(content, now=_NOW)

    assert "requester=old" not in pruned
    assert "requester=new" in pruned
