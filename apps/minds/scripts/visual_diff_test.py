"""Tests for the SPA visual-diff harness's fixture bootstrap.

``visual_diff.py capture-spa`` builds its bootstrap document out of the real
wire models in ``ui_models.py``, so a required field added upstream fails the
builder rather than silently producing a wrong-shaped bootstrap. That failure
is only ever seen by whoever runs the harness by hand, though -- it is
deliberately not wired into CI -- so the fixture rots until the next capture
attempt. This pins it instead: a pure model construction, no browser, no
server, no bundle.
"""

import visual_diff


def test_spa_fixture_bootstrap_satisfies_the_wire_models() -> None:
    bootstrap = visual_diff._build_spa_fixture_bootstrap()

    snapshot = bootstrap.snapshot
    assert len(snapshot.workspaces.workspaces) > 0
    assert len(snapshot.providers.providers) > 0
    assert len(snapshot.requests.request_ids) > 0
    # The feed the bell reads, whose absence broke the harness outright.
    assert len(snapshot.notifications.entries) > 0
    assert snapshot.notifications.unresolved_count > 0
    # The form the index page actually inlines, so a field that serializes
    # differently than it validates is caught here too.
    assert "notifications" in bootstrap.model_dump_json()
