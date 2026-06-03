import re
from pathlib import Path

import pytest

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client import templates as _templates_module
from imbue.minds.desktop_client.templates import render_auth_error_page
from imbue.minds.desktop_client.templates import render_chrome_page
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_dev_styleguide_page
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_recovery_page
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.desktop_client.testing import extract_ssr_route_payload
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId

_TOKENS_CSS_PATH = Path(_templates_module.__file__).resolve().parent / "static" / "tokens.css"

_AGENT_A: AgentId = AgentId("agent-00000000000000000000000000000001")
_AGENT_B: AgentId = AgentId("agent-00000000000000000000000000000002")


def test_render_login_redirect_page_inlines_one_time_code_for_client_hydration() -> None:
    html = render_login_redirect_page(one_time_code=OneTimeCode("abc123-secret-82341"))
    payload = extract_ssr_route_payload(html)
    assert payload["route"] == "login_redirect"
    assert payload["props"]["one_time_code"] == "abc123-secret-82341"


def test_render_auth_error_page_inlines_error_message_for_client_hydration() -> None:
    html = render_auth_error_page(message="This code has already been used.")
    payload = extract_ssr_route_payload(html)
    assert payload["route"] == "auth_error"
    assert payload["props"]["message"] == "This code has already been used."


def test_agent_id_rejects_invalid_format() -> None:
    with pytest.raises(InvalidRandomIdError):
        AgentId("not-a-valid-agent-id")


def test_agent_id_accepts_valid_format() -> None:
    agent_id = AgentId("agent-00000000000000000000000000000001")
    assert agent_id == "agent-00000000000000000000000000000001"


def test_render_create_form_has_default_values() -> None:
    html = render_create_form()
    assert "assistant" in html
    assert "forever-claude-template" in html
    assert "host_name" in html
    assert "launch_mode" in html


def test_render_create_form_prefills_values() -> None:
    html = render_create_form(git_url="https://custom/repo", host_name="my-workspace", branch="feature/test")
    assert "https://custom/repo" in html
    assert "my-workspace" in html
    assert "feature/test" in html


def test_render_create_form_contains_all_launch_modes() -> None:
    html = render_create_form()
    for mode in LaunchMode:
        assert mode.value.lower() in html


def test_render_create_form_selects_lima_by_default_without_account() -> None:
    # With no account selected the compute provider defaults to LIMA (the
    # local self-served default); IMBUE_CLOUD is only the default when an
    # account is present.
    html = render_create_form()
    assert 'value="LIMA" selected' in html


def test_render_create_form_selects_specified_launch_mode() -> None:
    # CLOUD instead of the default LIMA so the "selection honored over the
    # default" assertion is meaningful.
    html = render_create_form(launch_mode=LaunchMode.CLOUD)
    assert 'value="CLOUD" selected' in html
    assert 'value="LIMA" selected' not in html


def test_render_create_form_contains_ai_provider_options() -> None:
    html = render_create_form()
    for provider in AIProvider:
        assert f'value="{provider.value}"' in html


def test_render_create_form_defaults_ai_provider_to_subscription_without_account() -> None:
    html = render_create_form()
    assert 'value="SUBSCRIPTION" selected' in html


def test_render_create_form_omits_env_file_checkbox() -> None:
    html = render_create_form()
    assert "include_env_file" not in html


def test_render_create_form_shows_error_message_when_supplied() -> None:
    html = render_create_form(error_message="Imbue cloud requires an account.")
    assert "Imbue cloud requires an account." in html


def test_render_create_form_honors_workspace_env_vars_in_dev_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a dev tier, the MINDS_WORKSPACE_* env vars pre-fill the create form.

    Used by ``just minds-start`` to point the form at the operator's local
    FCT worktree + current branch so the dev-iteration loop is one click.
    """
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds-dev-josh")
    monkeypatch.setenv("MINDS_WORKSPACE_GIT_URL", "/local/fct/path")
    monkeypatch.setenv("MINDS_WORKSPACE_NAME", "mindtest")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "/local/fct/path" in html
    assert "mindtest" in html
    assert "mngr/some-feature" in html


def test_render_create_form_ignores_workspace_env_vars_in_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging must not honor MINDS_WORKSPACE_* env vars.

    Without the gate, a stray ``MINDS_WORKSPACE_BRANCH=mngr/some-branch`` in
    the operator's shell (e.g. left over from a prior ``just minds-start``
    invocation) would pre-fill the form's branch field and propagate to
    the imbue_cloud lease request as ``-b repo_branch_or_tag=...``, which
    would silently fail to match any pool host baked with the tier's
    canonical branch.
    """
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds-staging")
    monkeypatch.setenv("MINDS_WORKSPACE_GIT_URL", "/local/fct/path")
    monkeypatch.setenv("MINDS_WORKSPACE_NAME", "mindtest")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "/local/fct/path" not in html
    assert "mindtest" not in html
    assert "mngr/some-feature" not in html
    # And the hardcoded fallbacks DO appear (form is still usable).
    assert "forever-claude-template" in html
    assert "assistant" in html


def test_render_create_form_ignores_workspace_env_vars_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production -- like staging -- must not honor the dev-iteration env vars."""
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "mngr/some-feature" not in html


def test_render_create_form_ignores_workspace_env_vars_when_unactivated(monkeypatch: pytest.MonkeyPatch) -> None:
    """No activated env (no MINDS_ROOT_NAME) -- treat as non-dev and ignore env vars.

    Mirrors the conservative default: a bare ``minds run`` without any
    activation context shouldn't accidentally pull from ad-hoc env vars.
    """
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "mngr/some-feature" not in html


def test_render_login_page_emits_solid_route_payload() -> None:
    html = render_login_page()
    payload = extract_ssr_route_payload(html)
    assert payload["route"] == "login"
    assert payload["props"] == {}


def test_render_chrome_page_contains_titlebar() -> None:
    html = render_chrome_page()
    assert "minds-titlebar" in html
    assert "sidebar-toggle" in html
    assert "home-btn" in html
    assert "back-btn" in html
    assert "content-frame" in html


def test_render_chrome_page_hides_window_controls_on_mac() -> None:
    """On macOS, the window-controls row carries the 'hidden' Tailwind class
    so the native traffic lights are used instead."""
    html_mac = render_chrome_page(is_mac=True)
    html_other = render_chrome_page(is_mac=False)
    # The 'hidden' class only appears on the window-controls wrapper in
    # mac mode; on other platforms the same element is visible.
    assert 'class="flex hidden"' in html_mac or 'class="flex  hidden"' in html_mac
    assert 'class="flex hidden"' not in html_other and 'class="flex  hidden"' not in html_other


def test_render_chrome_page_shows_window_controls_on_non_mac() -> None:
    html = render_chrome_page(is_mac=False)
    assert "min-btn" in html
    assert "max-btn" in html
    assert "close-btn" in html


def test_render_sidebar_page_contains_workspace_list() -> None:
    html = render_sidebar_page()
    assert "sidebar-workspaces" in html
    # The interactivity (including the SSE EventSource fallback) now lives
    # in the external /_static/sidebar.js file; the template should pull it in.
    assert "/_static/sidebar.js" in html


def test_render_dev_styleguide_page_surfaces_tokens_and_macro_widgets() -> None:
    """The styleguide must surface the live ``:root`` tokens and render
    each catalog widget through its real macro (so the catalog can't drift
    silently from the macros it documents)."""
    html = render_dev_styleguide_page()
    assert "--shadow-seam" in html
    # The accent picker section is a separate runtime variable, not a :root token.
    assert "--workspace-accent" in html
    # Each pattern block should be present.
    for header in (
        "Titlebar buttons",
        "Window controls",
        "Sidebar items",
        "Accent spine",
        "Spinner",
        "Buttons",
        "Notices",
    ):
        assert header in html, f"missing pattern: {header}"
    # The buttons / notices / inputs are rendered through _macros.html; these
    # assertions verify that the macro output (button label, notice copy, input
    # name) actually reaches the rendered page.
    assert ">Primary<" in html and ">Danger<" in html
    assert "All set: action completed." in html
    assert 'name="styleguide-focus-ring-input"' in html


def test_dev_styleguide_token_swatches_enumerate_root_declarations() -> None:
    """Drift guard: every ``:root`` token in ``tokens.css`` must have a
    matching ``data-token`` swatch in the styleguide template (and vice
    versa). Failure means the catalog is out of sync with the live tokens.
    """
    root_block = re.search(r":root\s*\{([^}]*)\}", _TOKENS_CSS_PATH.read_text(), re.DOTALL)
    assert root_block is not None, "tokens.css must declare a :root block"
    declared = {f"--{name}" for name in re.findall(r"--([a-z][a-z0-9-]*)\s*:", root_block.group(1))}

    html = render_dev_styleguide_page()
    surfaced = set(re.findall(r'data-token="(--[a-z][a-z0-9-]*)"', html))

    assert declared == surfaced, (
        f"tokens.css :root declares {sorted(declared)} but the styleguide "
        f"surfaces {sorted(surfaced)}. Add or remove a "
        f'`data-token="--<name>"` swatch in templates/dev_styleguide.html '
        f"to match."
    )
