"""Unit tests for the SSR sidecar fallback path.

Exercises the ``render_*`` shims in ``templates.py``: when ``sidecar``
is ``None`` (or the sidecar's ``render`` raises), the shim returns a
deterministic client-render shell that hydrates the right route key
and props. Sidecar process-lifecycle integration with a running Node
process is out of scope for the current migration phase.
"""

from imbue.minds.desktop_client.ssr_sidecar import SsrSidecarError
from imbue.minds.desktop_client.templates import _client_render_shell
from imbue.minds.desktop_client.templates import render_ssr_or_fallback
from imbue.minds.desktop_client.testing import extract_ssr_route_payload


def test_client_render_shell_inlines_route_and_props() -> None:
    html = _client_render_shell(route="welcome", props={"hello": "world"})
    payload = extract_ssr_route_payload(html)
    assert payload == {"route": "welcome", "props": {"hello": "world"}}
    assert '<div id="app"></div>' in html
    assert 'src="/_static/_dist/assets/app.js"' in html


def test_client_render_shell_escapes_script_terminators() -> None:
    """``</script>`` and U+2028/U+2029 inside props must not break out of
    the JSON ``<script>`` block. The shell escapes them so the inlined
    payload can never close the script element prematurely.
    """
    html = _client_render_shell(
        route="auth_error",
        props={"message": "</script><script>alert(1)</script>"},
    )
    # The literal ``</script>`` must not appear inside the JSON payload --
    # it would let the inlined string close the inlining script tag.
    assert "</script><script>alert" not in html
    # The original string is still recoverable via JSON.parse on the client.
    payload = extract_ssr_route_payload(html)
    assert payload["props"]["message"] == "</script><script>alert(1)</script>"


def test_render_ssr_or_fallback_with_no_sidecar_returns_shell() -> None:
    html = render_ssr_or_fallback(sidecar=None, route="login", props={})
    payload = extract_ssr_route_payload(html)
    assert payload["route"] == "login"
    assert payload["props"] == {}


class _BoomSidecar:
    """A fake SsrSidecar that always raises -- exercises the fallback path."""

    def render(self, *, route: str, props: dict[str, object], bundle: str = "app") -> str:
        raise SsrSidecarError("sidecar exploded")


def test_render_ssr_or_fallback_with_failing_sidecar_returns_shell() -> None:
    html = render_ssr_or_fallback(sidecar=_BoomSidecar(), route="welcome", props={})
    payload = extract_ssr_route_payload(html)
    # The shell still embeds the route key so client hydration can take
    # over -- the user sees the page as soon as the JS bundle loads.
    assert payload["route"] == "welcome"


class _SuccessSidecar:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], str]] = []

    def render(self, *, route: str, props: dict[str, object], bundle: str = "app") -> str:
        self.calls.append((route, props, bundle))
        return f"<html><body>SSR for {route}</body></html>"


def test_render_ssr_or_fallback_with_healthy_sidecar_returns_ssr_html() -> None:
    sidecar = _SuccessSidecar()
    html = render_ssr_or_fallback(sidecar=sidecar, route="welcome", props={"a": 1})
    assert html == "<html><body>SSR for welcome</body></html>"
    assert sidecar.calls == [("welcome", {"a": 1}, "app")]
