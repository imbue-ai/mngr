import re
from pathlib import Path
from typing import Final

import pytest

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client import templates as _templates_module
from imbue.minds.desktop_client.templates import CATALOG
from imbue.minds.desktop_client.templates import render_auth_error_page
from imbue.minds.desktop_client.templates import render_chrome_page
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_dev_styleguide_page
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_recovery_page
from imbue.minds.desktop_client.templates import render_sharing_editor
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.desktop_client.templates import render_workspace_settings
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR_NAME
from imbue.minds.desktop_client.workspace_color import WORKSPACE_PALETTE
from imbue.minds.desktop_client.workspace_color import normalize_workspace_color
from imbue.minds.desktop_client.workspace_color import pick_unused_create_color
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId

# The hand-written Tailwind v4 source. Holds the :root design tokens (the
# styleguide cross-checks these) plus the component CSS; compiled to app.min.css.
_TOKENS_CSS_PATH = Path(_templates_module.__file__).resolve().parent / "static" / "app.css"

_AGENT_A: AgentId = AgentId("agent-00000000000000000000000000000001")
_AGENT_B: AgentId = AgentId("agent-00000000000000000000000000000002")


def test_render_landing_page_with_agents_lists_them_as_links() -> None:
    ids = (_AGENT_A, _AGENT_B)
    html = render_landing_page(accessible_agent_ids=ids)
    assert f"/goto/{_AGENT_A}/" in html
    assert f"/goto/{_AGENT_B}/" in html
    assert str(_AGENT_A) in html
    assert str(_AGENT_B) in html


def test_render_landing_page_settings_link_interpolates_agent_id() -> None:
    # Regression: the settings gear is a <Button> (JinjaX component), so its
    # onclick must use the `attr={{ expr }}` form -- a quoted `onclick="...{{ }}..."`
    # is forwarded literally, which sent `/workspace/{{ agent_id }}/settings` to the
    # server and 500'd the AgentId parse on destroy.
    html = render_landing_page(accessible_agent_ids=(_AGENT_A,))
    assert f"/workspace/{_AGENT_A}/settings" in html
    assert "{{" not in html


def test_render_landing_page_has_open_in_new_window_button_before_settings() -> None:
    # Each workspace row carries an "open in new window" arrow to the LEFT of
    # the settings gear. It calls window.landingOpenInNewWindow, which relays
    # to the main process in Electron (or opens a new tab in a browser).
    html = render_landing_page(accessible_agent_ids=(_AGENT_A,))
    assert "window.landingOpenInNewWindow(this)" in html
    # The diagonal shaft of the open-in-new arrow (Figma node 560-5109).
    assert '<path d="M18 6L6 18"/>' in html
    # It sits before the settings button within the row.
    assert html.index("window.landingOpenInNewWindow") < html.index(f"/workspace/{_AGENT_A}/settings")


def test_render_workspace_settings_data_agent_id_interpolates() -> None:
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
        servers=(),
    )
    assert f'data-agent-id="{_AGENT_A}"' in html
    assert "{{" not in html


def test_render_workspace_settings_renders_all_palette_swatches() -> None:
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
        servers=(),
        current_color="#0b292b",
    )
    # All palette swatches present, with the workspace's current color
    # marked as the checked radio so screen readers see the selection state.
    for hex_value in WORKSPACE_PALETTE.values():
        assert f'data-color="{hex_value}"' in html
    assert 'aria-checked="true"' in html
    # The hex input is pre-filled with the current saved color.
    assert 'value="#0b292b"' in html
    # A reachable workspace renders no disabled swatch (the counterpart
    # of the stale test below).
    assert "disabled></button>" not in html


def test_render_workspace_settings_picker_disabled_when_stale() -> None:
    """is_stale=True disables the picker controls so the user can't write
    a label against an unreachable host (would not be observable until
    provider recovery)."""
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
        servers=(),
        current_color="#0b292b",
        is_stale=True,
    )
    assert 'data-is-stale="true"' in html
    # Every swatch carries the real ``disabled`` attribute (ColorSwatch
    # renders it last, so a disabled swatch ends ``disabled></button>``).
    # Checking the attribute -- not just the substring "disabled" -- is
    # required because the swatch and pill class strings contain the
    # ``disabled:opacity-40`` utility on every render.
    assert html.count("disabled></button>") == len(WORKSPACE_PALETTE)
    # The hex input is disabled too: its tag ends with a standalone
    # ``disabled`` attribute right before the closing ``>``.
    hex_input_tag = re.search(r'<input[^>]*id="color-hex-input"[^>]*>', html)
    assert hex_input_tag is not None
    assert re.search(r"\sdisabled\s*>$", hex_input_tag.group(0))


def test_render_workspace_settings_marks_no_swatch_selected_for_custom_hex() -> None:
    """When the saved color is a custom hex (not in the palette), no
    swatch shows as selected; the hex pill carries the value and the
    blue selection ring class instead."""
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
        servers=(),
        current_color="#123456",
    )
    assert 'value="#123456"' in html
    assert 'aria-checked="true"' not in html
    assert "is-selected" in html


def test_render_workspace_settings_pill_not_selected_for_palette_color() -> None:
    """When the saved color matches a palette entry, the swatch is the
    selected control -- the hex pill must not also carry the ring."""
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
        servers=(),
        current_color="#0b292b",
    )
    assert 'aria-checked="true"' in html
    assert "is-selected" not in html


def test_render_sharing_editor_workspace_link_interpolates_agent_id() -> None:
    # Regression: the workspace <Link href="...{{ }}..."> must interpolate
    # (component quoted-attribute interpolation does not happen in JinjaX).
    html = render_sharing_editor(
        agent_id=str(_AGENT_A),
        service_name="svc",
        title="Share",
        mngr_forward_origin="http://localhost:8421",
        ws_name="ws",
    )
    assert f"/goto/{_AGENT_A}/" in html
    assert "{{" not in html


def test_render_landing_page_with_no_agents_shows_empty_state() -> None:
    html = render_landing_page(accessible_agent_ids=())
    assert "No projects yet" in html


def test_render_landing_page_discovering_shows_auto_refresh() -> None:
    html = render_landing_page(accessible_agent_ids=(), is_discovering=True)
    assert "Discovering agents" in html
    assert "reload" in html
    assert "No projects yet" not in html
    assert "/goto/" not in html


def test_render_login_redirect_page_contains_redirect_script() -> None:
    html = render_login_redirect_page(
        one_time_code=OneTimeCode("abc123-secret-82341"),
    )
    assert "window.location.href" in html
    # The URL is built at runtime with encodeURIComponent, so the code appears
    # as a JS string literal (via Jinja's `tojson` filter) rather than inlined
    # into the URL directly.
    assert "abc123-secret-82341" in html
    assert "/authenticate?one_time_code=" in html
    assert "encodeURIComponent" in html


def test_render_auth_error_page_shows_error_message() -> None:
    html = render_auth_error_page(message="This code has already been used.")
    assert "This code has already been used." in html
    assert "Authentication Failed" in html
    assert "restart the server" in html


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
    # VULTR instead of the default LIMA so the "selection honored over the
    # default" assertion is meaningful.
    html = render_create_form(launch_mode=LaunchMode.VULTR)
    assert 'value="VULTR" selected' in html
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


def test_render_create_form_includes_color_picker_with_palette_swatches() -> None:
    html = render_create_form()
    # All palette swatches present.
    for hex_value in WORKSPACE_PALETTE.values():
        assert f'data-color="{hex_value}"' in html
    # Hidden input named "color" carries the default selection.
    assert 'name="color"' in html
    assert f'value="{DEFAULT_WORKSPACE_COLOR}"' in html


def test_render_create_form_marks_default_color_as_checked() -> None:
    html = render_create_form()
    # The default ``confusion`` swatch is the only one with aria-checked=true.
    assert html.count('aria-checked="true"') == 1


def test_render_create_form_pre_selects_provided_color() -> None:
    html = render_create_form(color="#cecd0c")
    # The hidden input + the matching swatch's aria-checked carry the
    # picked color.
    assert 'value="#cecd0c"' in html
    assert html.count('aria-checked="true"') == 1


def test_render_create_form_shows_error_message_when_supplied() -> None:
    html = render_create_form(error_message="Imbue cloud requires an account.")
    assert "Imbue cloud requires an account." in html


def test_render_create_form_honors_workspace_env_vars_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the explicit opt-in, the MINDS_WORKSPACE_* env vars pre-fill the form.

    Used by ``just minds-start`` (and the e2e runner) to point the form at the
    operator's local FCT worktree + current branch so the dev-iteration loop is
    one click.
    """
    monkeypatch.setenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", "1")
    monkeypatch.setenv("MINDS_WORKSPACE_GIT_URL", "/local/fct/path")
    monkeypatch.setenv("MINDS_WORKSPACE_NAME", "mindtest")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "/local/fct/path" in html
    assert "mindtest" in html
    assert "mngr/some-feature" in html


def test_render_create_form_honors_workspace_env_vars_on_staging_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-in is tier-independent: it works even on a shared tier (staging).

    Regression test: staging previously dropped MINDS_WORKSPACE_* unconditionally,
    so ``just minds-start`` against staging silently fell back to the public
    GitHub FCT on ``main`` -- meaning local FCT changes could never be tested
    against staging.
    """
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds-staging")
    monkeypatch.setenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", "1")
    monkeypatch.setenv("MINDS_WORKSPACE_GIT_URL", "/local/fct/path")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "/local/fct/path" in html
    assert "mngr/some-feature" in html


def test_render_create_form_ignores_workspace_env_vars_without_opt_in_on_shared_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the opt-in, a stray MINDS_WORKSPACE_* in the shell is ignored.

    A stray ``MINDS_WORKSPACE_BRANCH=mngr/some-branch`` (e.g. left over from a
    prior ``just minds-start``) must not pre-fill the form's branch field for an
    end-user ``minds run``, where it would propagate to the imbue_cloud lease as
    ``-b repo_branch_or_tag=...`` and fail to match any pool host baked with the
    tier's canonical branch.
    """
    monkeypatch.delenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", raising=False)
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


def test_render_create_form_ignores_workspace_env_vars_without_opt_in_on_dev_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier no longer matters: even a dev-tier root name ignores the vars without opt-in.

    This closes the old gap where dev tiers honored a stray MINDS_WORKSPACE_*
    purely by tier, with no explicit operator intent.
    """
    monkeypatch.delenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", raising=False)
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds-dev-josh")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "mngr/some-feature" not in html


def test_render_login_page_shows_prompt() -> None:
    html = render_login_page()
    assert "login URL" in html.lower() or "Login" in html


def test_render_chrome_page_contains_titlebar() -> None:
    html = render_chrome_page()
    assert "minds-titlebar" in html
    assert "sidebar-toggle" in html
    assert "home-btn" in html
    assert "back-btn" in html
    assert "content-frame" in html


def test_render_chrome_page_drops_title_swatch_and_seam_border() -> None:
    # The full-width accent bar replaces the small swatch and the
    # ``border-b border-white/10`` seam: the rounded content corner
    # already provides separation below.
    html = render_chrome_page()
    assert 'id="title-swatch"' not in html
    # The seam class shouldn't appear on the titlebar element. Other
    # uses of border-white/10 elsewhere on the page are fine; assert
    # on the specific titlebar markup.
    titlebar_open = html.index('id="minds-titlebar"')
    titlebar_close = html.index(">", titlebar_open)
    titlebar_tag = html[titlebar_open:titlebar_close]
    assert "border-b" not in titlebar_tag
    assert "border-white" not in titlebar_tag


def test_render_chrome_page_titlebar_background_follows_titlebar_bg_var() -> None:
    # The titlebar paints via the ``--titlebar-bg`` CSS variable (set by
    # chrome.js when a workspace is active) with a pure-white fallback, so
    # the neutral, workspace-less chrome transitions cleanly to the active
    # workspace's accent color.
    html = render_chrome_page()
    assert "var(--titlebar-bg" in html


def test_render_chrome_page_page_title_uses_text_primary_token() -> None:
    # The page title is a plain ``text-primary`` token; the ``.titlebar-surface``
    # scope re-bases that token off --titlebar-bg, so the title flips
    # black/white with the accent's lightness (in pure CSS).
    html = render_chrome_page()
    assert 'id="page-title" class="text-primary' in html


def test_render_chrome_page_account_button_lives_in_sidebar() -> None:
    # The titlebar no longer carries an account button (``id="user-btn"``); the
    # "Manage account(s)" / "Log in" entry now lives in the floating sidebar
    # alongside the workspace list and the "New workspace" CTA. The titlebar
    # accent color therefore doesn't have to repaint the account button -- the
    # sidebar's own dark background is constant.
    html = render_chrome_page()
    assert 'id="user-btn"' not in html
    assert 'id="sidebar-account"' in html
    assert 'id="sidebar-account-label"' in html


def test_render_chrome_page_content_iframe_uses_12px_rounded_corners() -> None:
    # 12px radius (``rounded-[12px]``) matches Electron-side
    # ``contentView.setBorderRadius(12)`` (= ``CONTENT_CORNER_RADIUS`` in
    # electron/main.js) so both modes render the same tucked-under shape
    # against the OS's outer window rounding. It is a structural exception to
    # the 4-step radius scale (2/4/8/16) -- pinned as an arbitrary value so it
    # stays locked to the Electron constant rather than tracking ``rounded-xl``.
    html = render_chrome_page()
    iframe_open = html.index('id="content-frame"')
    iframe_close = html.index(">", iframe_open)
    iframe_tag = html[iframe_open:iframe_close]
    assert "rounded-[12px]" in iframe_tag


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
    # The floating-menu wrapper id. The sidebar runs inside the shared
    # modal WebContentsView, which covers the full window content area and
    # acts as a modal: sidebar.js compares click targets against
    # ``#sidebar-menu`` to distinguish clicks inside the floating panel
    # (let the menu's own handlers run) from clicks on the transparent
    # backdrop outside it (dismiss the modal). Renaming or dropping this id
    # breaks the click-outside-to-close behavior.
    assert 'id="sidebar-menu"' in html
    # SidebarBottom.jinja is rendered inside the floating menu in both
    # Chrome.jinja (browser mode) and Sidebar.jinja (the sidebar page loaded
    # into the shared modal WebContentsView in Electron). It carries the
    # "New workspace" CTA and the "Manage account(s)" / "Log in" entry; the
    # label is updated dynamically by sidebar.js from /auth/api/status.
    assert 'id="sidebar-new-workspace"' in html
    assert 'id="sidebar-account"' in html
    assert 'id="sidebar-account-label"' in html


def test_render_sidebar_page_position_tracks_trigger_anchor() -> None:
    """The floating menu's left/top come from the caller's trigger rect
    + offset (caller passes the trigger button's viewport-relative rect
    and a chosen offset; the menu anchors at trigger.bottom-left + offset).
    The chrome view and the modal view share window coordinate space, so
    the rect translates directly. This replaces an earlier ``is_mac``
    branch -- the position is now driven by call-site geometry rather
    than baked into a server template.

    Trigger rect (72, 0, 32, 28) is roughly the macOS sidebar-toggle
    button (traffic-light-shifted titlebar with a w-8 h-7 button). A
    non-default offset (0, 8) is passed here to prove the value flows
    through: the menu anchors at left=72+0=72, top=0+28+8=36."""
    html = render_sidebar_page(
        trigger_x=72,
        trigger_y=0,
        trigger_w=32,
        trigger_h=28,
        offset_x=0,
        offset_y=8,
    )
    assert "left:72px" in html
    assert "top:36px" in html

    # Defaults (no caller args) anchor a 38px-tall element at the top-left,
    # nudged 2px left (offset_x=-2 -> 0 + -2) and 2px below it
    # (offset_y=2 -> 0 + 38 + 2) -- right shape for "open the sidebar from
    # the first titlebar button" without any caller customization.
    html_default = render_sidebar_page()
    assert "left:-2px" in html_default
    assert "top:40px" in html_default


def test_render_sidebar_page_menu_width_is_280px() -> None:
    html = render_sidebar_page()
    assert "w-[280px]" in html
    assert "w-[244px]" not in html


def test_render_recovery_page_includes_agent_id_and_return_to() -> None:
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="http://agent.localhost:8421/",
        initial_status="stuck",
        initial_error="",
    )
    assert str(_AGENT_A) in html
    assert "http://agent.localhost:8421/" in html
    assert "/api/agents/" in html
    # The two restart tiers the recovery page can dispatch.
    assert "restart-system-interface" in html
    assert "restart-host" in html
    # The layer-2 probe endpoint the page calls on load.
    assert "host-health" in html
    assert 'data-initial-status="stuck"' in html


def test_render_recovery_page_restarting_status() -> None:
    html = render_recovery_page(
        agent_id=_AGENT_B,
        return_to="",
        initial_status="restarting",
        initial_error="",
    )
    assert 'data-initial-status="restarting"' in html


def test_render_recovery_page_carries_restart_failed_error() -> None:
    html = render_recovery_page(
        agent_id=_AGENT_B,
        return_to="",
        initial_status="restart_failed",
        initial_error="Start step of host restart failed: exited 1",
    )
    assert 'data-initial-status="restart_failed"' in html
    assert "Start step of host restart failed: exited 1" in html


def test_render_recovery_page_includes_diagnostics_dom_hooks() -> None:
    """The recovery page must expose the DOM hooks the JS uses to render the
    debug-menu details block and the Copy diagnostics button. The hooks are
    present on every render -- the JS populates them when the host-health
    endpoint response arrives.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    assert 'id="recovery-debug-details"' in html
    assert 'id="recovery-debug-content"' in html
    assert 'id="copy-diagnostics-btn"' in html


def test_render_recovery_page_renders_copy_ssh_button_with_command() -> None:
    """When given an ssh_command, the page renders a Copy SSH command button
    that carries the exact command in its data attribute, beside Copy diagnostics.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
        ssh_command="ssh -i /home/user/.mngr/key -p 60022 root@127.0.0.1",
    )
    assert 'id="copy-ssh-btn"' in html
    assert 'data-ssh-command="ssh -i /home/user/.mngr/key -p 60022 root@127.0.0.1"' in html
    # The button must sit inside the diagnostics menu, alongside Copy diagnostics.
    diag_pos = html.index('id="copy-diagnostics-btn"')
    ssh_pos = html.index('id="copy-ssh-btn"')
    details_pos = html.index('id="recovery-debug-details"')
    assert details_pos < diag_pos < ssh_pos
    # The click handler copies the data attribute to the clipboard.
    assert "data-ssh-command" in html
    assert "navigator.clipboard" in html


def test_render_recovery_page_omits_copy_ssh_button_without_command() -> None:
    """With no ssh_command (the default), the Copy SSH command button is absent
    -- we never render an inert button that would copy nothing.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    assert 'id="copy-ssh-btn"' not in html
    assert "Copy SSH command" not in html
    # Copy diagnostics is unaffected.
    assert 'id="copy-diagnostics-btn"' in html


def test_render_recovery_page_script_branches_on_dispatch_tier() -> None:
    """The recovery page reads ``dispatch_tier`` directly off the host-health response.

    Each restart tier the server may report must have a corresponding
    code branch in the page's JS.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    assert "dispatch_tier" in html
    for tier in ("'workspace_misconfigured'", "'host_offline'", "'interface_unresponsive'", "'host_unresponsive'"):
        assert tier in html, f"recovery page JS missing branch for {tier}"
    # The shared landing places for each branch.
    assert "renderMisconfigured" in html
    assert "renderUnresponsive" in html
    assert "Workspace misconfigured" in html
    assert "Try restart anyway" in html


def test_render_recovery_page_loading_hides_diagnostic_dropdown() -> None:
    """renderLoading must hide the diagnostic dropdown so a stale prior diagnostic
    does not linger on the page while a fresh check is in flight (issue: user
    clicked Restart workspace and the previous probe's diagnostic stayed open).
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    # renderLoading clears the cached payload and hides the debug details.
    loading_block_start = html.find("function renderLoading")
    assert loading_block_start >= 0
    loading_block_end = html.find("function ", loading_block_start + 1)
    loading_block = html[loading_block_start:loading_block_end]
    assert "show(debugDetailsEl, false)" in loading_block
    assert "latestHealth = null" in loading_block


def test_render_recovery_page_restart_failed_also_runs_probe() -> None:
    """The restart_failed entry must run the diagnostic probe so the page
    shows both the error details and the diagnostics (in separate elements),
    not just the error.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="restart_failed",
        initial_error="Stop step of host restart failed: exited 1",
    )
    # The restart_failed branch in the dispatcher calls runProbe(false) so
    # the diagnostics are populated without auto-dispatching another restart.
    assert "restart_failed" in html
    assert "runProbe(false)" in html
    # The error-details DOM hook is rendered alongside the diagnostic.
    assert 'id="recovery-error"' in html
    assert 'id="recovery-debug-details"' in html


def test_render_recovery_page_honors_misconfigured_before_autodispatch_short_circuit() -> None:
    """The workspace_misconfigured tier must be honored on the restart_failed path.

    A workspace whose services.toml lacks [services.system_interface] lands in
    restart_failed once its undeclared interface fails to come back up, so the
    page runs runProbe(false). If the no-auto-dispatch short-circuit
    (``if (!autoDispatch) renderUnresponsive()``) ran before the
    workspace_misconfigured check, that workspace would render a misleading
    "unresponsive" page even though no restart can recover it. Assert the
    misconfigured branch precedes the short-circuit inside runProbe so the
    restart_failed path still reaches renderMisconfigured().
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="restart_failed",
        initial_error="boom",
    )
    probe_body = html[html.index("function runProbe(") :]
    misconfigured_pos = probe_body.index("'workspace_misconfigured'")
    short_circuit_pos = probe_body.index("if (!autoDispatch)")
    assert misconfigured_pos < short_circuit_pos, (
        "the workspace_misconfigured branch must precede the !autoDispatch short-circuit "
        "so a misconfigured workspace on the restart_failed path renders misconfigured"
    )


def test_render_recovery_page_promotes_button_above_troubleshooting() -> None:
    """The restart button is the page's primary action, so it must appear
    before the de-emphasized troubleshooting block -- not sandwiched between
    the error and diagnostics disclosures as in the previous layout. Both
    disclosures live inside that troubleshooting block.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="restart_failed",
        initial_error="boom",
    )
    button_pos = html.index('id="recovery-host-btn"')
    block_pos = html.index('class="recovery-troubleshooting"')
    error_pos = html.index('id="recovery-error"')
    debug_pos = html.index('id="recovery-debug-details"')
    # Button first, then the troubleshooting block, then both disclosures.
    assert button_pos < block_pos < error_pos < debug_pos


def test_render_dev_styleguide_page_surfaces_tokens_and_component_widgets() -> None:
    """The styleguide must surface the live ``:root`` tokens and render
    each catalog widget through its real JinjaX component (so the catalog
    can't drift silently from the components it documents)."""
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
        "Color swatches",
        "Spinner",
        "Buttons",
        "Notices",
    ):
        assert header in html, f"missing pattern: {header}"
    # The buttons / notices / inputs are rendered through their JinjaX
    # components (Button, Notice, TextInput); these assertions verify that
    # the component output (button label, notice copy, input name) actually
    # reaches the rendered page.
    assert ">Primary<" in html and ">Danger<" in html
    assert "All set: action completed." in html
    assert 'name="styleguide-accent-input"' in html


def test_dev_styleguide_token_swatches_enumerate_design_tokens() -> None:
    """Drift guard: every design token in ``app.css`` must have a matching
    ``data-token`` swatch in the styleguide template (and vice versa). Failure
    means the catalog is out of sync with the live tokens.

    Design tokens are the Tailwind color tokens registered in ``@theme``
    (``--color-*``) plus the standalone ``--shadow-seam``. The raw value layer
    (``--c-*``) and the runtime-set chrome variables (``--workspace-accent`` /
    ``--titlebar-*``) are implementation detail behind the tokens and are
    intentionally NOT surfaced.
    """
    css = _TOKENS_CSS_PATH.read_text()
    # ``--color-*: ...`` declarations only (the @theme token layer); the
    # border-compat shim's ``var(--color-gray-200, ...)`` is a reference, not a
    # declaration, so it is not matched.
    declared = set(re.findall(r"(--color-[a-z0-9-]+)\s*:", css))
    # Standalone :root design tokens that aren't part of the @theme color set.
    declared |= {tok for tok in ("--shadow-seam",) if re.search(rf"{tok}\s*:", css)}

    html = render_dev_styleguide_page()
    surfaced = set(re.findall(r'data-token="(--[a-z][a-z0-9-]*)"', html))

    assert declared == surfaced, (
        f"app.css design tokens {sorted(declared)} but the styleguide "
        f"surfaces {sorted(surfaced)}. Add or remove a "
        f'`data-token="--<name>"` swatch in templates/pages/DevStyleguide.jinja '
        f"to match."
    )


# -- JinjaX component-level tests ----------------------------------------
#
# These exercise each individual component in isolation through the shared
# CATALOG so we catch regressions in any one component without rendering a
# whole page.


def test_button_link_renders_anchor_with_href() -> None:
    html = CATALOG.render("ButtonLink", href="/create", _content="Create")
    # attrs.render() sorts attributes alphabetically, so href ends up after
    # class. Assert presence rather than ordering.
    assert html.startswith("<a ")
    assert 'href="/create"' in html
    assert ">Create</a>" in html


def test_button_renders_each_variant_class_set() -> None:
    # The five variants should each contribute their own background class.
    variants_to_class = {
        "primary": "bg-surface-inverse",
        "secondary": "bg-fill-subtle",
        "danger": "bg-important/10",
        "success": "bg-success",
        "ghost": "bg-transparent",
    }
    for variant, css_class in variants_to_class.items():
        html = CATALOG.render("Button", variant=variant, _content="X")
        assert css_class in html, f"variant={variant} missing {css_class}"


def test_button_submit_has_form_attribute_when_passed() -> None:
    html = CATALOG.render("ButtonSubmit", form="my-form", _content="Save")
    assert 'type="submit"' in html
    assert 'form="my-form"' in html


def test_button_default_size_uses_md_geometry() -> None:
    html = CATALOG.render("Button", variant="primary", _content="X")
    # md size = px-3.5 py-2 rounded-md font-medium text-sm
    assert "px-3.5" in html
    assert "py-2" in html
    assert "rounded-md" in html
    assert "font-medium" in html
    assert "text-sm" in html
    # Should not pick up lg-specific classes
    assert "py-3" not in html
    assert "rounded-lg" not in html
    assert "font-semibold" not in html


def test_button_size_lg_uses_block_cta_geometry() -> None:
    html = CATALOG.render("Button", variant="primary", size="lg", block=True, _content="Sign in")
    assert "py-3" in html
    assert "rounded-lg" in html
    assert "font-semibold" in html
    assert "text-base" in html
    assert "w-full" in html


def test_button_size_icon_uses_square_padding() -> None:
    html = CATALOG.render("Button", variant="ghost", size="icon", _content="<svg/>")
    assert "p-1.5" in html
    # No horizontal/vertical padding mismatch (only one padding utility)
    assert "px-3.5" not in html
    assert "py-2 " not in html and not html.rstrip().endswith("py-2")


def test_button_passes_through_arbitrary_attrs() -> None:
    # JinjaX attrs.render() flows through undeclared HTML attributes like
    # title, aria-label, and data-*, so callers don't have to enumerate
    # them as props on the component.
    html = CATALOG.render(
        "Button",
        variant="ghost",
        size="icon",
        _content="<svg/>",
        _attrs={"title": "Restart", "aria-label": "Restart workspace", "data-x": "y"},
    )
    assert 'title="Restart"' in html
    assert 'aria-label="Restart workspace"' in html
    assert 'data-x="y"' in html


def test_color_swatch_renders_radio_contract() -> None:
    """The ColorSwatch component owns the markup contract the picker JS
    selects on: role=radio, data-color, aria-label, aria-checked, the
    .color-swatch class, and the background-color style."""
    html = CATALOG.render("ColorSwatch", hex="#0b292b", name="confusion", selected=True, size="md")
    assert 'role="radio"' in html
    assert 'data-color="#0b292b"' in html
    assert 'aria-label="confusion"' in html
    assert 'aria-checked="true"' in html
    assert "color-swatch" in html
    # The style sets the swatch fill; assert the trailing-semicolon form
    # (from ``background-color: {{ hex }};``) so the value is pinned and
    # the trailing-comment ratchet does not misfire on the hex literal.
    assert "#0b292b;" in html
    # md size geometry.
    assert "w-[34px]" in html
    assert "h-[34px]" in html


def test_color_swatch_unselected_and_small_and_disabled() -> None:
    html = CATALOG.render("ColorSwatch", hex="#cecd0c", name="energy", selected=False, size="sm", disabled=True)
    assert 'aria-checked="false"' in html
    # sm size geometry (create form).
    assert "w-6" in html
    assert "h-6" in html
    assert "disabled" in html


def test_titlebar_button_default_is_nav_variant() -> None:
    html = CATALOG.render("TitlebarButton", _content="<svg/>")
    # nav variant => w-8 h-7 rounded-md, default tone => plain foreground
    # tokens (text-secondary + hover:text-primary + hover:bg-fill-hover),
    # re-based per-workspace by the .titlebar-surface scope in app.css.
    assert "w-8" in html
    assert "h-7" in html
    assert "rounded-md" in html
    assert "text-secondary" in html
    assert "hover:bg-fill-hover" in html
    # The danger tone modifier should NOT be present on the default tone.
    assert "titlebar-btn-danger" not in html
    # Window-control geometry should NOT bleed into nav
    assert "w-9" not in html
    assert "h-[38px]" not in html


def test_titlebar_button_control_variant_renders_window_control_geometry() -> None:
    html = CATALOG.render("TitlebarButton", variant="control", _content="<svg/>")
    assert "w-9" in html
    assert "h-[38px]" in html
    assert "rounded-none" in html


def test_titlebar_button_danger_tone_applies_red_hover() -> None:
    html = CATALOG.render("TitlebarButton", variant="control", tone="danger", _content="<svg/>")
    # ``.titlebar-btn-danger`` (in app.css) supplies the red hover.
    assert "titlebar-btn-danger" in html
    # The shared foreground tokens still apply (base colors + geometry).
    assert "text-secondary" in html


# -- Workspace palette + WCAG contrast picker ----------------------------
#
# The palette is the user-pickable set of workspace colors. It lives
# server-side only (``WORKSPACE_PALETTE`` in workspace_color.py): the
# pickers render server-side swatches carrying data-color attributes,
# and the SSE workspaces payload emits the resolved accent. The titlebar
# derives its contrasting foreground from that accent in pure CSS (see
# .titlebar-surface in app.css). static/workspace_accent.js keeps just
# the ``normalizeHex`` runtime helper; the guard test below ensures no
# JS palette mirror gets reintroduced.

# Order is significant: it drives the picker's render order and
# pick_unused_create_color's preference walk. ``confusion`` (the
# default) leads; pure black and pure white are intentionally absent
# (the neutral system-theme chrome would collide with them).
_EXPECTED_PALETTE: Final[dict[str, str]] = {
    "confusion": "#0b292b",
    "courage": "#492222",
    "envy": "#3c3d06",
    "peace": "#9fbbd3",
    "belonging": "#e8a7a8",
    "energy": "#cecd0c",
    "strength": "#cfc7b3",
    "comfort": "#f5d6a0",
    "inspiration": "#e9ecd9",
    "clarity": "#fcefd4",
}

_WORKSPACE_ACCENT_JS_PATH = Path(_templates_module.__file__).resolve().parent / "static" / "workspace_accent.js"


def test_workspace_palette_matches_expected_entries() -> None:
    # Pinning the exact entries *and their order* here so a stray edit to
    # workspace_color.py (rename / typo / dropped entry / reorder) fails
    # loudly -- order drives both the picker's render order and
    # pick_unused_create_color's preference walk, so an order-insensitive
    # dict comparison would let a reorder slip through.
    assert list(WORKSPACE_PALETTE.items()) == list(_EXPECTED_PALETTE.items())


def test_workspace_palette_excludes_pure_black_and_white() -> None:
    # Pure black/white were removed so a workspace accent can't collide
    # with the neutral system-theme chrome (which is now pure white in
    # light mode / pure black in dark mode). Users can still type either
    # into the settings hex input; they're just not preset swatches.
    values = set(WORKSPACE_PALETTE.values())
    assert "#000000" not in values
    assert "#ffffff" not in values
    # ``confusion`` (the default) still leads the palette.
    assert list(WORKSPACE_PALETTE.keys())[0] == "confusion"


def test_default_workspace_color_is_confusion() -> None:
    assert DEFAULT_WORKSPACE_COLOR_NAME == "confusion"
    assert DEFAULT_WORKSPACE_COLOR == WORKSPACE_PALETTE["confusion"]
    assert DEFAULT_WORKSPACE_COLOR == "#0b292b"


def test_workspace_accent_js_has_no_palette_mirror() -> None:
    """The palette lives server-side only (workspace_color.py) and
    reaches the client as server-rendered swatches with data-color
    attributes. A JS palette literal would be a second source of truth
    to keep in sync; this guard fails if someone reintroduces one.
    The JS file keeps only the ``normalizeHex`` runtime helper -- the
    titlebar derives its contrasting foreground in pure CSS now."""
    js_content = _WORKSPACE_ACCENT_JS_PATH.read_text()
    assert "WORKSPACE_PALETTE" not in js_content
    assert "normalizeHex" in js_content


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#ffffff", "#ffffff"),
        ("ffffff", "#ffffff"),
        ("#FFFFFF", "#ffffff"),
        ("FFFFFF", "#ffffff"),
        ("#fff", "#ffffff"),
        ("fff", "#ffffff"),
        ("#FFF", "#ffffff"),
        ("#0b292b", "#0b292b"),
        ("0B292B", "#0b292b"),
        ("  #fff  ", "#ffffff"),
        ("\tffffff\n", "#ffffff"),
    ],
)
def test_normalize_workspace_color_accepts_lenient_inputs(value: str, expected: str) -> None:
    assert normalize_workspace_color(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-hex",
        "#ff",
        "#fffff",
        "#fffffff",
        "#xyz",
        "#ffffff80",
        "rgb(255, 255, 255)",
        "ffffffff",
    ],
)
def test_normalize_workspace_color_rejects_malformed_inputs(value: str) -> None:
    assert normalize_workspace_color(value) is None


# -- pick_unused_create_color --------------------------------------------
#
# The create form preselects the first palette color not already used by
# an existing workspace, falling back to confusion when nothing is in use
# yet or every palette entry is taken.

_PALETTE_HEXES: Final[tuple[str, ...]] = tuple(WORKSPACE_PALETTE.values())
_CONFUSION = WORKSPACE_PALETTE["confusion"]


def test_pick_unused_create_color_defaults_to_confusion_when_none_used() -> None:
    # No workspaces yet -> the named default (confusion, which also leads
    # the palette).
    assert pick_unused_create_color(set()) == _CONFUSION


def test_pick_unused_create_color_returns_confusion_when_all_used() -> None:
    assert pick_unused_create_color(set(_PALETTE_HEXES)) == _CONFUSION


def test_pick_unused_create_color_returns_first_unused_in_palette_order() -> None:
    # Confusion is used (e.g. one label-less workspace renders as confusion);
    # the first unused palette entry in order is courage (confusion leads
    # the chromatic block, so the next one is courage -- not a neutral).
    assert pick_unused_create_color({_CONFUSION}) == WORKSPACE_PALETTE["courage"]


def test_pick_unused_create_color_skips_to_next_unused() -> None:
    # confusion + courage taken -> next chromatic palette entry is envy.
    assert pick_unused_create_color({_CONFUSION, WORKSPACE_PALETTE["courage"]}) == WORKSPACE_PALETTE["envy"]


def test_pick_unused_create_color_ignores_custom_colors() -> None:
    # A custom (non-palette) color in use doesn't block any palette pick;
    # with a custom color the set is non-empty so the first palette entry
    # (confusion) is returned.
    assert pick_unused_create_color({"#123456"}) == _CONFUSION


def test_pick_unused_create_color_is_case_insensitive() -> None:
    # Uppercased used colors still match palette entries.
    used = {_CONFUSION.upper()}
    assert pick_unused_create_color(used) == WORKSPACE_PALETTE["courage"]


def test_app_css_defines_titlebar_self_theming() -> None:
    """Drift guard: the titlebar self-themes via the ``.titlebar-surface``
    scope, which re-bases the foreground tokens off --titlebar-bg in pure CSS
    (lch relative color). app.css must define it (+ the red close hover)."""
    css = _TOKENS_CSS_PATH.read_text()
    assert ".titlebar-surface" in css
    assert ".titlebar-btn-danger" in css
    # The contrast base is derived from --titlebar-bg via relative color.
    assert "lch(from var(--titlebar-bg)" in css


def test_tokens_css_drops_page_workspace_top_stripe() -> None:
    """The 3px ``.page-workspace::before`` stripe is now redundant with
    the colored chrome bar above; app.css must not redeclare it."""
    css = _TOKENS_CSS_PATH.read_text()
    assert ".page-workspace::before" not in css


def test_tokens_css_accent_fallback_is_default_workspace_color() -> None:
    """``--workspace-accent`` may not be set on some surfaces (e.g. the
    dev styleguide, or a sidebar item rendered before the SSE workspaces
    payload arrives), so the CSS rule includes a fallback. Pin the
    fallback to ``DEFAULT_WORKSPACE_COLOR`` (the palette's ``confusion``
    entry) so the un-applied state matches the migration backfill /
    create-time default."""
    css = _TOKENS_CSS_PATH.read_text()
    # Legacy OKLCH fallbacks must not linger.
    assert "oklch(" not in css
    # All fallbacks should use the palette default.
    assert f"var(--workspace-accent, {DEFAULT_WORKSPACE_COLOR})" in css


def test_no_legacy_oklch_accents_remain_in_templates_or_static() -> None:
    """The SHA-derived OKLCH accent system is gone: workspace accents are
    stored ``#rrggbb`` hexes, and every fallback / demo surface paints
    the palette default. Scan the hand-written template and static-asset
    trees so a lingering (or reintroduced) ``oklch(`` literal fails loudly;
    any future legitimate oklch use should be a conscious decision recorded
    by updating this guard.

    The compiled ``app.min.css`` is excluded: it is a generated, gitignored
    build artifact, and Tailwind v4 defines its entire default palette in
    ``oklch()`` -- so the scan targets only authored source, not output."""
    client_root = Path(_templates_module.__file__).resolve().parent
    offenders = [
        str(path.relative_to(client_root))
        for directory in (client_root / "templates", client_root / "static")
        for path in sorted(directory.rglob("*"))
        if path.suffix in (".jinja", ".js", ".css") and path.name != "app.min.css" and "oklch(" in path.read_text()
    ]
    assert offenders == []


def test_notice_renders_each_variant() -> None:
    variants_to_class = {
        "info": "bg-info/12",
        "warn": "bg-warning/12",
        "success": "bg-success/12",
        "error": "bg-important/12",
    }
    for variant, css_class in variants_to_class.items():
        html = CATALOG.render("Notice", variant=variant, _content="msg")
        assert css_class in html
        assert "msg" in html


def test_card_renders_default_slot() -> None:
    html = CATALOG.render("Card", _content="<p>body</p>")
    assert "<p>body</p>" in html
    # The visual shell (bg/border/rounded; no baseline shadow) is in the
    # ``.minds-card`` CSS class in app.css; the rendered HTML carries
    # the class name rather than the underlying Tailwind utilities.
    assert "minds-card" in html
    # Default padding is "default" -> p-4.
    assert "p-4" in html


def test_card_row_spread_layout_adds_justify_between() -> None:
    html = CATALOG.render("Card", layout="row-spread", _content="x")
    assert "justify-between" in html
    assert "items-center" in html
    assert "gap-1.5" in html


def test_card_row_layout_omits_justify_between() -> None:
    html = CATALOG.render("Card", layout="row", _content="x")
    assert "items-center" in html
    assert "justify-between" not in html
    # Row children sit at a tight gap-1.5 (6px), not the old gap-3.
    assert "gap-1.5" in html
    assert "gap-3" not in html


def test_card_tight_padding_uses_px4_py25() -> None:
    html = CATALOG.render("Card", padding="tight", _content="x")
    assert "px-4" in html
    assert "py-2.5" in html
    assert "p-4 " not in html and not html.rstrip().endswith("p-4")


def test_card_tag_anchor_renders_anchor_with_href() -> None:
    html = CATALOG.render("Card", tag="a", href="/x", _content="body")
    assert "<a " in html
    assert 'href="/x"' in html
    # Anchors auto-disable underline + inherit text color so a Card anchor
    # doesn't read like a regular hyperlink.
    assert "no-underline" in html
    assert "text-inherit" in html


def test_card_interactive_adds_hover_classes() -> None:
    plain = CATALOG.render("Card", _content="x")
    interactive = CATALOG.render("Card", interactive=True, _content="x")
    assert "hover:border-strong" not in plain
    assert "hover:border-strong" in interactive
    assert "cursor-pointer" in interactive


def test_form_label_default_is_block_with_mb_1_5() -> None:
    # The prop is ``target`` rather than ``for`` because JinjaX parses
    # the prop declaration block as a Python function signature, and
    # ``for`` is a reserved keyword. The rendered HTML still uses the
    # standard HTML ``for`` attribute.
    html = CATALOG.render("FormLabel", target="email", _content="Email")
    assert 'for="email"' in html
    assert "block" in html
    assert "mb-1.5" in html
    assert "text-sm" in html
    assert "font-medium" in html
    assert "text-primary" in html


def test_form_label_inline_drops_block_and_mb() -> None:
    html = CATALOG.render("FormLabel", target="x", inline=True, _content="Provider")
    # Inline layout: no block / mb classes (the parent flex row handles
    # spacing), but the shared color and weight tokens remain.
    assert "block" not in html
    assert "mb-1.5" not in html
    assert "text-sm" in html
    assert "font-medium" in html


def test_oauth_button_renders_google_label_and_brand_icon_with_hook_class() -> None:
    html = CATALOG.render("auth.OauthButton", provider="google")
    # The .oauth-btn hook is load-bearing -- static/auth.js queries for
    # it to enable/disable all OAuth buttons as a group.
    assert "oauth-btn" in html
    # Label text + data-oauth provider attr.
    assert "Continue with Google" in html
    assert 'data-oauth="google"' in html
    # Brand glyph from auth.OauthIcon is composed inline. The path
    # fragment is one of the four <path d="..."> values unique to
    # Google's blue triangle.
    assert "M22.56 12.25" in html


def test_oauth_button_github_uses_github_label_and_glyph() -> None:
    html = CATALOG.render("auth.OauthButton", provider="github")
    assert "Continue with GitHub" in html
    assert 'data-oauth="github"' in html
    # Path fragment that opens GitHub's mark glyph.
    assert "M12 0C5.37 0 0 5.37" in html


def test_card_page_default_padding_and_max_width() -> None:
    html = CATALOG.render("CardPage", title="x", _content="<p>body</p>")
    # Card surface: bg/border/rounded/shadow + p-10 + max-w-[420px] + w-full.
    assert "bg-surface-primary" in html
    assert "rounded-lg" in html
    assert "shadow-sm" in html
    assert "p-10" in html
    assert "max-w-[420px]" in html
    assert "<p>body</p>" in html
    # The body is flex-centered around the card.
    assert "flex items-center justify-center min-h-screen" in html


def test_card_page_form_padding_uses_p6() -> None:
    html = CATALOG.render("CardPage", title="x", padding="form", max_width="max-w-[520px]", _content="x")
    assert "p-6" in html
    assert "p-10" not in html
    assert "max-w-[520px]" in html


def test_icon24_renders_with_stroke_shell_and_default_size() -> None:
    # ``home`` is one of the icons in the ICONS_24 catalog global.
    html = CATALOG.render("Icon24", name="home")
    # Stroke-based shell attrs applied uniformly.
    assert 'viewBox="0 0 24 24"' in html
    assert 'fill="none"' in html
    assert 'stroke="currentColor"' in html
    assert 'stroke-width="2"' in html
    assert 'aria-hidden="true"' in html
    # Default size = md = w-4 h-4.
    assert "w-4 h-4" in html
    # Path data from the catalog flows through unescaped.
    assert '<path d="M3 12L12 3l9 9"/>' in html


def test_icon24_size_axis() -> None:
    for size, css_class in (("sm", "w-3.5 h-3.5"), ("md", "w-4 h-4"), ("lg", "w-5 h-5")):
        html = CATALOG.render("Icon24", name="home", size=size)
        assert css_class in html


def test_icon24_renders_arrow_up_right() -> None:
    # The diagonal open-in-new arrow (Figma node 560-5109) backs the
    # "open in new window" affordance on workspace rows (landing page + sidebar).
    html = CATALOG.render("Icon24", name="arrow-up-right")
    assert 'viewBox="0 0 24 24"' in html
    assert '<path d="M18 16.5V6H7.5"/>' in html
    assert '<path d="M18 6L6 18"/>' in html


def test_icon24_renders_menu() -> None:
    # The lucide ``menu`` glyph (three horizontal lines) is the titlebar
    # button that opens the floating workspace menu.
    html = CATALOG.render("Icon24", name="menu")
    assert 'viewBox="0 0 24 24"' in html
    assert '<line x1="4" y1="6" x2="20" y2="6"/>' in html
    assert '<line x1="4" y1="12" x2="20" y2="12"/>' in html
    assert '<line x1="4" y1="18" x2="20" y2="18"/>' in html


def test_icon12_renders_with_w3_h3_size_and_12_viewbox() -> None:
    html = CATALOG.render("Icon12", name="close")
    assert 'viewBox="0 0 12 12"' in html
    assert "w-3 h-3" in html
    # Two lines forming the X.
    assert '<line x1="2" y1="2" x2="10" y2="10"/>' in html
    assert '<line x1="10" y1="2" x2="2" y2="10"/>' in html


def test_spinner_renders_for_each_size() -> None:
    for size, css_class in (("sm", "w-3.5"), ("md", "w-[18px]"), ("lg", "w-8")):
        html = CATALOG.render("Spinner", size=size)
        assert 'class="spinner' in html
        assert css_class in html


def test_spinner_default_tone_omits_accent_class() -> None:
    html = CATALOG.render("Spinner", size="sm")
    assert "spinner-accent" not in html


def test_spinner_accent_tone_adds_accent_class() -> None:
    html = CATALOG.render("Spinner", size="sm", tone="accent")
    assert "spinner-accent" in html


def test_oauth_icon_google_includes_google_svg_path() -> None:
    html = CATALOG.render("auth.OauthIcon", provider="google")
    # One of the four <path d="..."> values unique to the Google glyph
    # (the blue triangle); shows the right SVG was selected.
    assert "M22.56 12.25" in html


def test_oauth_icon_github_includes_github_svg_path() -> None:
    html = CATALOG.render("auth.OauthIcon", provider="github")
    # The opening of GitHub's mark path.
    assert "M12 0C5.37 0 0 5.37" in html


def test_oauth_icon_unknown_provider_renders_nothing_visible() -> None:
    # Defensive: the icon component has no fallback path, so an unexpected
    # provider just produces empty output (no exception).
    html = CATALOG.render("auth.OauthIcon", provider="not-a-provider").strip()
    assert html == ""


def test_text_input_default_radius_is_md() -> None:
    html = CATALOG.render("TextInput", name="email")
    assert "rounded-md" in html
    assert "rounded-lg" not in html


def test_text_input_radius_lg_for_auth_cards() -> None:
    html = CATALOG.render("TextInput", name="email", radius="lg")
    assert "rounded-lg" in html
    assert "rounded-md" not in html


def test_text_input_autocomplete_and_minlength_pass_through() -> None:
    html = CATALOG.render(
        "TextInput",
        name="password",
        type="password",
        radius="lg",
        autocomplete="new-password",
        minlength=8,
    )
    assert 'autocomplete="new-password"' in html
    assert 'minlength="8"' in html


def test_text_input_omits_autocomplete_and_minlength_when_unset() -> None:
    html = CATALOG.render("TextInput", name="email")
    assert "autocomplete=" not in html
    assert "minlength=" not in html


def test_text_input_passes_through_arbitrary_attrs() -> None:
    # attrs.render() flows undeclared HTML attributes (readonly, onkeydown,
    # data-*) so callers don't enumerate each as a prop.
    html = CATALOG.render(
        "TextInput",
        name="email",
        _attrs={"id": "new-email", "onkeydown": "addEmail()", "data-x": "y"},
    )
    assert 'id="new-email"' in html
    assert 'onkeydown="addEmail()"' in html
    assert 'data-x="y"' in html


def test_select_renders_with_option_children_and_focus_ring() -> None:
    html = CATALOG.render(
        "Select",
        name="launch_mode",
        _content='<option value="LIMA">lima</option>',
    )
    assert "<select" in html
    assert 'name="launch_mode"' in html
    assert '<option value="LIMA">lima</option>' in html
    # Inherits the shared INPUT_BASE accent focus ring.
    assert "focus:border-accent" in html
    assert "focus:ring-2" in html
    # Default width is w-full.
    assert "w-full" in html


def test_select_honors_width_prop() -> None:
    html = CATALOG.render("Select", name="x", width="w-48", _content="")
    assert "w-48" in html
    # Default w-full should be replaced, not added alongside.
    assert " w-full " not in html


def test_link_regular_uses_accent_underline_recipe() -> None:
    html = CATALOG.render("Link", href="/x", _content="back").strip()
    assert "<a " in html
    assert 'href="/x"' in html
    assert "text-accent" in html
    assert "hover:underline" in html
    assert "font-medium" not in html


def test_link_medium_weight_adds_font_medium() -> None:
    html = CATALOG.render("Link", href="/x", weight="medium", _content="Sign in")
    assert "font-medium" in html


def test_link_passes_through_arbitrary_attrs() -> None:
    html = CATALOG.render(
        "Link",
        href="https://example.com",
        _content="docs",
        _attrs={"target": "_blank", "rel": "noopener"},
    )
    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html


def test_textarea_renders_value_in_content_with_shared_shell() -> None:
    html = CATALOG.render(
        "Textarea",
        name="env",
        value="line1\nline2",
        rows=6,
        extra="font-mono",
    )
    assert "<textarea" in html
    assert 'name="env"' in html
    assert 'rows="6"' in html
    assert "line1\nline2" in html
    assert "font-mono" in html
    assert "focus:border-accent" in html


def test_section_header_plain_has_no_divider_classes() -> None:
    html = CATALOG.render("SectionHeader", _content="Account")
    assert "Account" in html
    assert "border-t" not in html
    assert "mt-8" not in html


def test_section_header_divider_renders_top_border() -> None:
    html = CATALOG.render("SectionHeader", divider=True, _content="Sharing")
    assert "Sharing" in html
    assert "border-t" in html
    assert "border-default" in html
    assert "mt-8" in html
    assert "pt-5" in html


def test_dialog_close_button_renders_x_svg_and_onclick() -> None:
    html = CATALOG.render("DialogCloseButton", onclick="closePermissionDialog()")
    assert 'aria-label="Close"' in html
    assert 'onclick="closePermissionDialog()"' in html
    # The X-glyph path data fragment that identifies the close SVG.
    assert "M4.22 4.22a.75.75 0 0 1 1.06 0L10 8.94" in html


def test_dialog_close_button_id_optional() -> None:
    without_id = CATALOG.render("DialogCloseButton", onclick="x()")
    with_id = CATALOG.render("DialogCloseButton", id="my-close", onclick="x()")
    assert "id=" not in without_id
    assert 'id="my-close"' in with_id


def test_modal_renders_hidden_overlay_with_default_card() -> None:
    html = CATALOG.render("Modal", id="my-dialog", _content="<p>body</p>")
    assert 'id="my-dialog"' in html
    assert "hidden fixed inset-0 z-50" in html
    assert "bg-surface-overlay" in html
    assert "<p>body</p>" in html


def test_modal_card_extra_appends_to_inner_card_classes() -> None:
    html = CATALOG.render("Modal", id="x", card_extra="text-left", _content="hi")
    # The card_extra value lands on the inner card div, NOT on the outer overlay.
    assert "text-left" in html


def test_status_badge_renders_each_variant_class_set() -> None:
    variants_to_class = {
        "neutral": "bg-fill-subtle",
        "success": "bg-success/15",
        "error": "bg-important/15",
        "warn": "bg-warning/15",
        "info": "bg-info/15",
    }
    for variant, css_class in variants_to_class.items():
        html = CATALOG.render("StatusBadge", variant=variant, _content="x")
        assert css_class in html, f"variant={variant} missing {css_class}"


def test_status_badge_size_xs_uses_text_xs() -> None:
    html = CATALOG.render("StatusBadge", size="xs", _content="x")
    assert "text-xs" in html
    assert "text-sm" not in html


def test_status_badge_title_renders_when_present() -> None:
    html = CATALOG.render("StatusBadge", title="why this is shown", _content="x")
    assert 'title="why this is shown"' in html


def test_status_badge_title_omitted_when_empty() -> None:
    html = CATALOG.render("StatusBadge", _content="x")
    assert "title=" not in html
