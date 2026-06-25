from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from imbue.minds.desktop_client.workspace_ssh import SshGrantError
from imbue.minds.desktop_client.workspace_ssh import build_authorized_keys_line
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
