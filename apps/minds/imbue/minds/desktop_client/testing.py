"""Shared non-fixture test helpers for desktop_client tests."""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from typing import TypedDict

# Matches the JSON payload that ``_client_render_shell`` and the Node SSR
# sidecar both emit so the client bundle can hydrate the right route.
# Kept in one place so callers can't drift if the shell ever changes shape.
_SSR_PAYLOAD_RE = re.compile(
    r'<script type="application/json" id="__route__">(.+?)</script>',
    re.DOTALL,
)


class SsrRoutePayload(TypedDict):
    """Shape of the JSON blob the SSR shell inlines for client hydration.

    ``route`` is the key the client's route registry uses to pick a Solid
    component. ``props`` are the per-route arguments rendered into the
    component; the per-route schema varies (e.g. ``login_redirect``
    carries a one-time code, ``auth_error`` carries an error message) so
    we leave the value type as ``Any`` to keep test assertions terse.
    """

    route: str
    props: dict[str, Any]


def extract_ssr_route_payload(html: str) -> SsrRoutePayload:
    """Return the ``{route, props}`` payload embedded in an SSR-shell page.

    Raises ``AssertionError`` if the payload script tag is missing. Used
    by every desktop_client test that asserts on which Solid route the
    server told the client to hydrate.
    """
    match = _SSR_PAYLOAD_RE.search(html)
    if match is None:
        raise AssertionError(f"No __route__ payload found in SSR shell: {html[:200]!r}")
    return json.loads(match.group(1))


def restic_backup_a_file(repository: str, password: str, source: Path) -> None:
    """Create one snapshot in ``repository`` from ``source`` using plain restic."""
    env = dict(os.environ)
    env.update({"RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD": password})
    result = subprocess.run(
        ["restic", "backup", str(source)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120.0,
    )
    assert result.returncode == 0, result.stderr
