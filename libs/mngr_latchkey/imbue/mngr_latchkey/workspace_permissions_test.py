"""Unit tests for the ``minds-workspaces`` verb metadata.

The grant *effect* (scope + per-verb schemas + rule) is computed in the gateway's
``permission_requests.mjs`` extension and is exercised by its end-to-end tests
(``permission_requests_test.py``); this module only covers the Python-side view
of the shared ``workspace_permissions.json`` catalog the desktop dialog renders
from.
"""

from imbue.mngr_latchkey.workspace_permissions import MINDS_WORKSPACES_SCOPE
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS
from imbue.mngr_latchkey.workspace_permissions import is_targeted_verb


def test_scope_is_minds_workspaces() -> None:
    assert MINDS_WORKSPACES_SCOPE == "minds-workspaces"


def test_is_targeted_verb_classifies_verbs() -> None:
    assert is_targeted_verb("minds-workspaces-destroy")
    assert is_targeted_verb("minds-workspaces-ssh")
    assert not is_targeted_verb("minds-workspaces-read")
    assert not is_targeted_verb("minds-workspaces-create")
    assert not is_targeted_verb("not-a-verb")


def test_all_verbs_have_dialog_metadata() -> None:
    for verb in WORKSPACE_VERBS:
        assert verb.permission.startswith("minds-workspaces")
        assert verb.display_name
        assert verb.description


def test_verb_catalog_covers_expected_verbs() -> None:
    names = {verb.permission for verb in WORKSPACE_VERBS}
    assert names == {
        "minds-workspaces-read",
        "minds-workspaces-create",
        "minds-workspaces-destroy",
        "minds-workspaces-lifecycle",
        "minds-workspaces-backups-export",
        "minds-workspaces-ssh",
    }


def test_targeted_verbs_are_the_mutating_and_ssh_verbs() -> None:
    targeted = {verb.permission for verb in WORKSPACE_VERBS if verb.is_targeted}
    assert targeted == {
        "minds-workspaces-destroy",
        "minds-workspaces-lifecycle",
        "minds-workspaces-backups-export",
        "minds-workspaces-ssh",
    }
