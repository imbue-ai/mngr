import re
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import Final

import pytest

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client import templates as _templates_module
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptInfo
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.onboarding_services import OnboardingService
from imbue.minds.desktop_client.templates import ADD_ACCOUNT_OPTION_VALUE
from imbue.minds.desktop_client.templates import CATALOG
from imbue.minds.desktop_client.templates import DEFAULT_EXPECTED_CREATE_ATTEMPT_DURATION_SECONDS
from imbue.minds.desktop_client.templates import FALLBACK_BRANCH
from imbue.minds.desktop_client.templates import InspirationWorkspaceRow
from imbue.minds.desktop_client.templates import expected_create_attempt_duration_seconds
from imbue.minds.desktop_client.templates import make_unique_host_name
from imbue.minds.desktop_client.templates import render_account_plan_modal_page
from imbue.minds.desktop_client.templates import render_account_plan_section
from imbue.minds.desktop_client.templates import render_accounts_modal_page
from imbue.minds.desktop_client.templates import render_accounts_page
from imbue.minds.desktop_client.templates import render_auth_error_page
from imbue.minds.desktop_client.templates import render_chrome_page
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_creating_page
from imbue.minds.desktop_client.templates import render_destroyed_workspaces_page
from imbue.minds.desktop_client.templates import render_destroyed_workspaces_rows_fragment
from imbue.minds.desktop_client.templates import render_dev_styleguide_page
from imbue.minds.desktop_client.templates import render_help_page
from imbue.minds.desktop_client.templates import render_inbox_page
from imbue.minds.desktop_client.templates import render_inspiration_create_page
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_overlay_host_page
from imbue.minds.desktop_client.templates import render_recovery_page
from imbue.minds.desktop_client.templates import render_sharing_editor
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.desktop_client.templates import render_workspace_backup_history
from imbue.minds.desktop_client.templates import render_workspace_options_modal_page
from imbue.minds.desktop_client.templates import render_workspace_options_page
from imbue.minds.desktop_client.templates import render_workspace_settings
from imbue.minds.desktop_client.templates import resolve_create_host_name
from imbue.minds.desktop_client.testing import is_workspace_options_pane_hidden
from imbue.minds.desktop_client.testing import workspace_options_pane_html
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR_NAME
from imbue.minds.desktop_client.workspace_color import WORKSPACE_PALETTE
from imbue.minds.desktop_client.workspace_color import normalize_workspace_color
from imbue.minds.desktop_client.workspace_color import pick_unused_create_color
from imbue.minds.mngr_settings.data_types import CloudAccountRecord
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import DockerRuntime
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import default_docker_runtime
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


def test_render_landing_page_settings_button_opens_the_options_panel() -> None:
    # The gear opens the docked options panel over the list rather than
    # navigating into the workspace's own settings page. The agent id is read
    # off the row at click time, so nothing is interpolated into the handler --
    # which is what the old `/workspace/<id>/settings` onclick got wrong twice.
    html = render_landing_page(accessible_agent_ids=(_AGENT_A,))
    assert "window.landingOpenWorkspaceOptions(this)" in html
    assert f"/workspace/{_AGENT_A}/settings" not in html
    assert "{{" not in html


def test_render_landing_page_has_open_in_new_window_button_before_settings() -> None:
    # Each workspace row carries an "open in new window" arrow to the LEFT of
    # the settings gear. It calls window.landingOpenInNewWindow, which relays
    # to the main process in Electron (or opens a new tab in a browser).
    html = render_landing_page(accessible_agent_ids=(_AGENT_A,))
    assert "window.landingOpenInNewWindow(this)" in html
    # The open-in-new arrow glyph (Icon16 ``arrow-up-right``, Figma node 857-5137).
    assert '<path d="M12.9331 10.3336' in html
    # It sits before the settings button within the row.
    assert html.index("window.landingOpenInNewWindow(this)") < html.index("window.landingOpenWorkspaceOptions(this)")


def test_render_workspace_settings_data_agent_id_interpolates() -> None:
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
    )
    assert f'data-agent-id="{_AGENT_A}"' in html
    assert "{{" not in html


def test_render_workspace_settings_view_all_links_to_backup_history_page() -> None:
    # workspace_backups.js toggles the "View all N backups" link and count.
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
    )
    assert f"/workspace/{_AGENT_A}/backups" in html


def test_render_workspace_settings_carries_the_restore_dialog() -> None:
    # The restore confirmation dialog ships in the page markup; the ids are
    # load-bearing (workspace_backups.js fills the time and drives the flow).
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
    )
    assert 'id="restore-dialog"' in html
    assert 'id="restore-dialog-time"' in html
    assert 'id="restore-cancel-btn"' in html
    assert 'id="restore-confirm-btn"' in html
    # The chained-update choice ships in the dialog, checked by default.
    assert 'id="restore-update-after-checkbox"' in html
    assert "checked" in html


def test_render_workspace_settings_puts_the_operation_strip_below_the_backups_table() -> None:
    # Every backup operation (restore, update, storage change) reports through
    # one shared strip placed right below the Recent backups table where
    # restores are launched, not inside the "Fix backup problems" section.
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
    )
    for element_id in (
        "backup-operation-strip",
        "backup-op-spinner",
        "backup-cancel-btn",
        "backup-stop-chats-btn",
        "backup-skip-safety-btn",
        "backup-force-restore-btn",
        "backup-op-progress",
        "backup-op-details-toggle",
        "backup-op-log",
        "backup-error",
        "backup-warning",
        "backup-success",
        "backup-cancelled",
    ):
        assert f'id="{element_id}"' in html
    assert html.index('id="backup-history-card"') < html.index('id="backup-operation-strip"')
    assert html.index('id="backup-operation-strip"') < html.index('id="backup-configure"')


def test_render_workspace_backup_history_page_shell() -> None:
    # The page is a shell filled client-side: it must carry the agent id for
    # workspace_backup_history.js, load that script, and link back to settings.
    # Restores run in place on this page, so it also ships the shared restore
    # dialog and tracked-operation strip (the same RestoreDialog and
    # BackupOperationStrip components as the settings page) plus the shared
    # driver script that wires them.
    html = render_workspace_backup_history(agent_id=str(_AGENT_A), ws_name="my-workspace")
    assert f'data-agent-id="{_AGENT_A}"' in html
    assert "workspace_backup_history.js" in html
    assert "backup_operation_ui.js" in html
    for element_id in (
        "restore-dialog",
        "restore-dialog-time",
        "restore-cancel-btn",
        "restore-confirm-btn",
        "restore-update-after-checkbox",
        "backup-operation-strip",
        "backup-op-spinner",
        "backup-op-progress",
        "backup-op-details-toggle",
        "backup-op-log",
        "backup-error",
        "backup-warning",
        "backup-success",
        "backup-cancelled",
        "backup-cancel-btn",
        "backup-stop-chats-btn",
        "backup-skip-safety-btn",
        "backup-force-restore-btn",
    ):
        assert f'id="{element_id}"' in html
    assert f"/workspace/{_AGENT_A}/settings" in html
    assert "my-workspace" in html
    assert "{{" not in html


def test_render_workspace_settings_renders_all_palette_swatches() -> None:
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
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
    assert "No machines yet" in html


def test_render_landing_page_discovering_shows_auto_refresh() -> None:
    html = render_landing_page(accessible_agent_ids=(), is_discovering=True)
    assert "Discovering agents" in html
    assert "reload" in html
    # Discovery may never surface the remembered workspaces (e.g. stale
    # last-good entries), so the discovering state must not be a dead end:
    # it always offers the create affordance.
    assert 'href="/create"' in html
    assert "No machines yet" not in html
    assert "/goto/" not in html


def test_render_landing_page_signed_out_launcher_signs_in_back_to_home() -> None:
    # Signed out (no account email): the bottom-left account launcher reads
    # "Log in", and (the Landing page being a trusted local page on the chrome
    # surface) it opens the sign-in modal via the shell bridge with
    # ``returnTo: '/'`` so a successful sign-in lands back on the home screen
    # (the server's return_to default is the create screen). It passes no
    # ``mode``, so the modal leads with sign-up -- a user with no account at
    # all needs to create one.
    html = render_landing_page(accessible_agent_ids=())
    assert 'id="landing-minds-settings"' in html
    assert 'id="landing-account"' in html
    assert "Log in" in html
    assert "window.minds.openSigninModal('/')" in html
    assert "window.minds.openSigninModal('/', 'signin')" not in html
    assert "'/auth/signup'" in html


def test_render_landing_page_signed_in_launcher_shows_email_and_extra_count() -> None:
    html = render_landing_page(
        accessible_agent_ids=(),
        account_email="alice@example.com",
        extra_account_count=2,
    )
    assert "alice@example.com" in html
    assert "(+2)" in html


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
    # The repository git URL still has a hardcoded fallback (in the advanced
    # view); the compute provider select is present.
    assert "default-workspace-template" in html
    assert "launch_mode" in html


def test_render_create_form_branch_default_pairs_with_default_repo() -> None:
    # With no explicit repository the branch input shows the operator/env
    # default (FALLBACK_BRANCH absent any env override).
    html = render_create_form()
    assert f'value="{FALLBACK_BRANCH}"' in html


def test_render_create_form_explicit_repo_keeps_branch_blank() -> None:
    # An explicitly-supplied repository (e.g. an inspiration deeplink's
    # git_url) must NOT inherit the default template's branch: the pinned
    # minds tag is meaningless on another repo, and a blank branch means
    # "the repo's latest version" at submit time (resolve_template_version).
    html = render_create_form(git_url="https://github.com/acme/inspiration")
    assert "https://github.com/acme/inspiration" in html
    assert FALLBACK_BRANCH not in html


def test_render_create_form_explicit_repo_keeps_explicit_branch() -> None:
    html = render_create_form(git_url="https://github.com/acme/inspiration", branch="feature-x")
    assert 'value="feature-x"' in html


def test_render_create_form_has_optional_name_field() -> None:
    # The advanced view exposes an explicit "Name" (host_name) field so a user
    # can name the workspace; left empty, the server auto-names it (workspace-N).
    html = render_create_form()
    assert 'name="host_name"' in html


def test_render_create_form_prefills_host_name() -> None:
    # A submitted name survives a validation-error re-render.
    html = render_create_form(host_name="my-mind")
    assert 'name="host_name"' in html
    assert 'value="my-mind"' in html


def test_render_create_form_shows_preset_cards() -> None:
    html = render_create_form()
    assert 'data-preset="remote"' in html
    assert 'data-preset="local"' in html
    assert "Imbue Cloud" in html
    assert "Directly on your computer" in html
    assert "Advanced Configuration" in html


def test_render_create_form_landing_fallback_wires_self_heal_sse() -> None:
    # The landing fallback (create form shown at "/" when no workspace is known)
    # subscribes to the chrome SSE and navigates to "/" once a workspace appears,
    # so a user stranded here by a cold-start discovery race is not trapped.
    html = render_create_form(is_landing_fallback=True)
    assert "/_chrome/events" in html
    assert "EventSource" in html
    assert "workspaceNowExists" in html
    assert "window.location = '/'" in html


def test_render_create_form_explicit_page_omits_self_heal_sse() -> None:
    # The explicit /create page (the default, is_landing_fallback False) must not
    # wire the self-heal SSE, so a deliberate "create another workspace" flow is
    # never bounced away by the user's existing workspaces.
    html = render_create_form()
    assert "/_chrome/events" not in html
    assert "workspaceNowExists" not in html


def test_render_create_form_opens_signin_modal_via_overlay_bridge() -> None:
    # Choosing Imbue Cloud while signed out opens the sign-in modal in the
    # desktop client's shared overlay layer (so it covers the title bar), not an
    # in-page dialog. The create page therefore no longer embeds the auth form
    # or loads auth.js itself; being a trusted local page on the chrome surface,
    # it asks the Electron main process to open the /auth/signin-modal page via
    # the window.minds shell bridge (falling back to navigating there directly in
    # the browser).
    html = render_create_form(accounts=[])
    assert "window.minds.openSigninModal()" in html
    assert "/auth/signin-modal" in html
    # The auth form + its script now live in the overlay page, not here.
    assert 'id="signin-modal"' not in html
    assert 'id="signin-form"' not in html
    assert "/_static/auth.js" not in html


def test_render_create_form_has_account_picker_error_element() -> None:
    # A signed-in user who selects Imbue Cloud but "No account" is shown a red
    # account-picker error (toggled client-side); the element must be present.
    html = render_create_form()
    assert 'id="account-error"' in html
    assert "text-important" in html


def test_render_create_form_does_not_redirect_on_card_click() -> None:
    # The old behavior redirected to the sign-in page on card click / used a
    # "Sign in & create" submit label. Both are gone: card click only selects,
    # and the button stays "Create".
    html = render_create_form(accounts=[])
    assert "SIGNIN_URL" not in html
    assert "Sign in & create" not in html


def test_render_create_form_prefills_values() -> None:
    html = render_create_form(git_url="https://custom/repo", branch="feature/test")
    assert "https://custom/repo" in html
    assert "feature/test" in html


def test_render_create_form_contains_all_launch_modes() -> None:
    html = render_create_form()
    # GCP / AZURE are bring-your-own-key-account-only modes: they are reachable
    # solely through configured cloud-account options (``BYOK:<block>``), never
    # rendered as ambient compute options.
    byok_only_modes = {LaunchMode.AWS, LaunchMode.GCP, LaunchMode.AZURE}
    for mode in LaunchMode:
        if mode in byok_only_modes:
            assert f'<option value="{mode.value}"' not in html
            continue
        # Assert on the option's ``value=`` attribute (the exact enum value),
        # not the visible text: Modal renders a friendly label instead of the
        # lowercased value (it shows "Modal (1-day ephemeral)").
        assert f'value="{mode.value}"' in html


def test_render_create_form_selects_imbue_cloud_compute_by_default() -> None:
    # A fresh form defaults to the remote ("Imbue Cloud") preset regardless of
    # whether an account is signed in, so the compute provider starts on
    # IMBUE_CLOUD rather than the local LIMA default.
    html = render_create_form()
    assert 'value="IMBUE_CLOUD" selected' in html
    assert 'value="LIMA" selected' not in html


def test_render_create_form_selects_specified_launch_mode() -> None:
    # VULTR instead of the default LIMA so the "selection honored over the
    # default" assertion is meaningful.
    html = render_create_form(launch_mode=LaunchMode.VULTR)
    assert 'value="VULTR" selected' in html
    assert 'value="LIMA" selected' not in html


def test_render_create_form_has_no_ai_provider_field() -> None:
    # AI provider selection moved into the workspace's own sign-in modal;
    # the create form must not offer it (or an API-key input) anymore.
    html = render_create_form()
    assert "ai_provider" not in html
    assert "anthropic_api_key" not in html


def test_render_create_form_contains_docker_runtime_options() -> None:
    html = render_create_form()
    for runtime in DockerRuntime:
        assert f'value="{runtime.value}"' in html


def test_render_create_form_defaults_docker_runtime_to_platform_value() -> None:
    # The runtime select pre-selects the platform-appropriate default (runc on
    # macOS, runsc on Linux) so the form works out of the box on either host.
    html = render_create_form()
    assert f'value="{default_docker_runtime().value}" selected' in html


def test_render_create_form_selects_specified_docker_runtime() -> None:
    # Pick the runtime that is NOT this platform's default so the "selection
    # honored over the default" assertion is meaningful on both macOS and Linux.
    non_default = DockerRuntime.RUNSC if default_docker_runtime() is DockerRuntime.RUNC else DockerRuntime.RUNC
    html = render_create_form(docker_runtime=non_default)
    assert f'value="{non_default.value}" selected' in html


def test_render_create_form_local_preset_selects_lima() -> None:
    # Selecting the local preset (e.g. a re-render of a LIMA submission) keeps
    # the compute provider on the local LIMA default.
    html = render_create_form(selected_preset="local")
    assert 'value="LIMA" selected' in html
    assert 'aria-checked="true"' in _preset_card_tag(html, "local")


def test_render_create_form_omits_env_file_checkbox() -> None:
    html = render_create_form()
    assert "include_env_file" not in html


def test_render_create_form_carries_color_in_hidden_input_without_swatches() -> None:
    # The color is auto-chosen, so there is no visible palette picker; a hidden
    # ``color`` input carries the selection through the POST.
    html = render_create_form()
    assert 'name="color"' in html
    assert f'value="{DEFAULT_WORKSPACE_COLOR}"' in html
    # No visible swatches (the palette picker markup is gone).
    assert "color-swatch" not in html
    for hex_value in WORKSPACE_PALETTE.values():
        assert f'data-color="{hex_value}"' not in html


def test_render_create_form_carries_provided_color_in_hidden_input() -> None:
    html = render_create_form(color="#cecd0c")
    assert 'value="#cecd0c"' in html


def _preset_card_tag(html: str, preset: str) -> str:
    """Return the opening ``<button>`` tag for the given preset card.

    Attribute order is whatever JinjaX's ``attrs.render`` emits, so callers
    check attributes by membership within the tag rather than by position.
    """
    match = re.search(r'<button[^>]*data-preset="' + preset + r'"[^>]*>', html)
    assert match is not None, f"no preset card for {preset!r}"
    return match.group(0)


def test_render_create_form_default_preset_is_remote_without_account() -> None:
    # The remote ("Imbue Cloud") preset is the default even with no account
    # signed in; a no-account user is nudged toward signing in via the card
    # click, not by flipping the default to local.
    html = render_create_form()
    assert 'aria-checked="true"' in _preset_card_tag(html, "remote")
    assert 'aria-checked="false"' in _preset_card_tag(html, "local")


def test_render_create_form_default_preset_is_remote_with_account() -> None:
    acct = SimpleNamespace(user_id="u-1", email="a@b.com")
    html = render_create_form(accounts=[acct], default_account_id="u-1")
    assert 'aria-checked="true"' in _preset_card_tag(html, "remote")
    assert 'aria-checked="false"' in _preset_card_tag(html, "local")
    # Selection styling is driven by the aria-checked Tailwind variant on the
    # PresetCard, not a server-toggled class.
    assert "aria-checked:outline-accent" in html


def test_render_create_form_preset_cards_use_badge_check_icons() -> None:
    # The feature checklists use the badge-check glyphs rather than a plain
    # check: the remote (Imbue Cloud) card shows the *filled* badge
    # (``badge-check-filled`` -- the lone evenodd-knockout glyph) in the accent
    # (blue) color, and the local card the *unfilled* outline badge
    # (``badge-check``) with no color class of its own, so it inherits the
    # adjacent feature text's color. Both render at the native 16px (``w-4``),
    # each nudged down 2px (``mt-0.5``) to sit on the text line. Icons render to
    # raw path data, so scope each card's region and assert on the icon-span
    # signature plus the glyph fingerprints.
    html = render_create_form()
    remote_region = html[html.index('data-preset="remote"') : html.index('data-preset="local"')]
    local_region = html[html.index('data-preset="local"') : html.index('id="advanced-view"')]
    # Remote: accent (blue) filled badge -- the only glyph with an evenodd
    # knockout. ``shrink-0 mt-0.5`` pins the assertion to the icon span.
    assert "text-accent shrink-0 mt-0.5" in remote_region
    assert 'fill-rule="evenodd"' in remote_region
    # Local: outline badge whose span carries only layout classes (no text-*),
    # so it inherits the feature line's color. No filled-badge knockout.
    assert 'class="shrink-0 mt-0.5"' in local_region
    assert "text-secondary shrink-0 mt-0.5" not in local_region
    assert "M14.0635 7.99966" in local_region
    assert 'fill-rule="evenodd"' not in local_region
    # Both badges render at the native 16px (md = w-4), not the small 14px (sm).
    assert "w-3.5 h-3.5" not in remote_region + local_region
    assert "w-4 h-4" in remote_region and "w-4 h-4" in local_region


def test_render_create_form_start_advanced_opens_advanced_view() -> None:
    # ``start_advanced`` drives the inline init so the advanced view shows first.
    assert "showAdvanced(true)" in render_create_form(start_advanced=True)
    assert "showAdvanced(false)" in render_create_form(start_advanced=False)


def test_render_create_form_shows_error_message_when_supplied() -> None:
    html = render_create_form(error_message="Imbue cloud requires an account.")
    assert "Imbue cloud requires an account." in html


def test_render_creating_page_renders_onboarding_walkthrough() -> None:
    """The creating page carries the five-step onboarding walkthrough.

    The title, the step markers (1..5), the intro panel + advance button,
    and the graphics (minds logo, browser demo, app cloud, and the final
    latchkey/tunnel illustration) must all be present on first paint.
    """
    create_attempt_id = CreateAttemptId()
    info = AgentCreateAttemptInfo(
        create_attempt_id=create_attempt_id,
        status=AgentCreateAttemptStatus.INITIALIZING,
        launch_mode=LaunchMode.DOCKER,
    )
    services = [
        OnboardingService(
            service_id="slack",
            display_name="Slack",
            icon_url="/_static/service_icons/slack.svg",
            icon_data_uri="data:image/svg+xml;base64,PHN2Zy8+",
        ),
        OnboardingService(service_id="ramp", display_name="Ramp", icon_url=None, icon_data_uri=None),
    ]
    html = render_creating_page(create_attempt_id=create_attempt_id, info=info, onboarding_services=services)
    assert "Setting up your machine" in html
    # Eight walkthrough steps: the intro, the chat, the tabs demo, the apps
    # cloud, the connections scene, the devices, publishing, and the tips.
    for step_number in range(1, 9):
        assert f'data-step="{step_number}"' in html
    assert 'data-step="9"' not in html
    assert 'id="intro-panel"' not in html
    # The progress strip is always visible.
    strip_index = html.index('id="top-strip"')
    strip_tag = html[html.rindex("<div", 0, strip_index) : html.index(">", strip_index)]
    assert "hidden" not in strip_tag
    # Readiness enters the workspace by itself, so there is no Begin button
    # and the details toggle stays put.
    assert 'id="top-begin"' not in html
    assert 'id="details-toggle"' in html
    # Nav is the dot strip alone: the walkthrough plays itself, so the old
    # Learn more / Previous / Next buttons are gone. One dot per step, each a
    # circle carrying the sweep arc.
    assert 'id="onboarding-advance"' not in html
    assert 'id="onboarding-prev"' not in html
    assert html.count('class="onboarding-dot"') == 8
    # The current step's dot stretches into a pill whose fill times the dwell.
    assert "onboarding-dot-fill" in html
    # The strip is dots alone: no play/pause/replay control.
    assert 'id="onboarding-control"' not in html
    # Graphics for the phases.
    for gfx in (
        'id="gfx-minds"',
        'id="gfx-chat"',
        'id="gfx-browser"',
        'id="gfx-apps"',
        'id="gfx-connect"',
        'id="gfx-devices"',
        'id="gfx-publish"',
    ):
        assert gfx in html
    # The app cloud + its spinning app wheel (fed from the services catalog),
    # and the latchkey mark on the final protection line.
    assert "app-cloud" in html
    assert "cloud-wheel" in html
    # The wheel is fed a JSON block of catalog entries whose icons are inlined
    # as data URIs, so it needs no network; services with no shipped brand icon
    # (Ramp here) are skipped.
    assert 'id="cloud-apps"' in html
    assert '"icon": "data:image/svg+xml;base64,' in html
    assert '"name": "Slack"' in html
    assert "/_static/service_icons/" not in html
    assert "Ramp" not in html
    # The label that pops the centered app's name in.
    assert "cloud-wheel-name" in html
    # The connections step: a permission request, the pointer that approves
    # it, and the button that becomes the link to the machine.
    assert "connect-card" in html
    assert "connect-approve" in html
    assert "connect-cursor" in html
    # The machine is drawn as a laptop, on this step and the sharing one.
    assert html.count('href="#laptop"') == 2
    # The old carousel is gone.
    assert "service-marquee" not in html


def test_render_creating_page_opens_on_the_minds_intro() -> None:
    """Every creation opens the same way: the title + gray progress strip and
    the minds intro as step one of a walkthrough that plays itself, with no
    button to press. Errors surface immediately."""
    create_attempt_id = CreateAttemptId()
    info = AgentCreateAttemptInfo(
        create_attempt_id=create_attempt_id,
        status=AgentCreateAttemptStatus.INITIALIZING,
        launch_mode=LaunchMode.DOCKER,
    )
    html = render_creating_page(create_attempt_id=create_attempt_id, info=info)
    assert 'data-surface-errors="true"' in html
    assert "This is Minds: your machine for building personalized apps." in html
    # Every step is two lines: what the thing is, then what you do with it.
    assert "Learn more while you wait." in html
    # The minds mark is defined once and referenced wherever it is shown.
    assert 'id="minds-mark"' in html
    assert html.count('href="#minds-mark"') == 1
    # Both devices show the same miniature of the app pane.
    assert html.count('href="#app-ui"') == 2
    # Nothing invites a click to start it any more.
    assert "Learn more while you wait?" not in html
    # The tips sit on their own final step rather than crowding the picture.
    tips_index = html.index('data-step="8"')
    assert 'id="tip"' in html[tips_index : html.index("</div>", html.index('id="tip"'))]


def test_render_creating_page_final_copy_matches_launch_mode() -> None:
    """The final step explains where the workspace runs, per launch mode."""
    create_attempt_id = CreateAttemptId()
    local_info = AgentCreateAttemptInfo(
        create_attempt_id=create_attempt_id,
        status=AgentCreateAttemptStatus.INITIALIZING,
        launch_mode=LaunchMode.DOCKER,
    )
    local_html = render_creating_page(create_attempt_id=create_attempt_id, info=local_info)
    assert "runs locally, so your computer has to be on" in local_html
    assert 'data-is-remote="false"' in local_html

    remote_info = AgentCreateAttemptInfo(
        create_attempt_id=create_attempt_id,
        status=AgentCreateAttemptStatus.INITIALIZING,
        launch_mode=LaunchMode.IMBUE_CLOUD,
    )
    remote_html = render_creating_page(create_attempt_id=create_attempt_id, info=remote_info)
    assert "even when your laptop is closed" in remote_html
    assert 'data-is-remote="true"' in remote_html


def test_render_creating_page_carries_hidden_github_auth_guidance() -> None:
    """The creating page ships the private-repo guidance as static, hidden
    content: creating.js reveals it only when the create-operation status
    reports error_kind GITHUB_AUTH_REQUIRED. It must name the GitHub CLI sign-in
    command, link the official docs, and offer the local-path alternative."""
    create_attempt_id = CreateAttemptId()
    info = AgentCreateAttemptInfo(
        create_attempt_id=create_attempt_id,
        status=AgentCreateAttemptStatus.INITIALIZING,
        launch_mode=LaunchMode.DOCKER,
    )
    html = render_creating_page(create_attempt_id=create_attempt_id, info=info)
    assert 'id="github-auth-help"' in html
    assert "gh auth login" in html
    assert "https://docs.github.com/en/github-cli/github-cli/quickstart" in html
    assert "path in the form instead of the URL" in html
    # Hidden on first paint -- the block only shows for the classified failure.
    guidance_index = html.index('id="github-auth-help"')
    tag_end = html.index(">", guidance_index)
    assert "hidden" in html[guidance_index:tag_end]


def test_render_creating_page_carries_hidden_generic_git_auth_guidance() -> None:
    """The creating page also ships generic (non-GitHub) git-auth guidance,
    revealed for error_kind GIT_AUTH_REQUIRED. It offers the local-path
    alternative but must NOT name the GitHub CLI (which only fits github.com)."""
    create_attempt_id = CreateAttemptId()
    info = AgentCreateAttemptInfo(
        create_attempt_id=create_attempt_id,
        status=AgentCreateAttemptStatus.INITIALIZING,
        launch_mode=LaunchMode.DOCKER,
    )
    html = render_creating_page(create_attempt_id=create_attempt_id, info=info)
    assert 'id="git-auth-help"' in html
    assert "path in the form instead of the URL" in html
    # Hidden on first paint.
    guidance_index = html.index('id="git-auth-help"')
    tag_end = html.index(">", guidance_index)
    assert "hidden" in html[guidance_index:tag_end]
    # The generic block must not carry the GitHub-CLI advice. Scope the check
    # to this block (the sibling github-auth-help block legitimately has it).
    block_end = html.index("</div>", guidance_index)
    assert "gh auth login" not in html[guidance_index:block_end]


def test_render_creating_page_failure_view_carries_a_dismiss_button() -> None:
    """The failure view ships a Dismiss button (revealed with the view by
    creating.js when the create attempt fails): it deletes the failed create attempt's
    pending record and in-memory row so the "Create failed" workspace-list row
    can be removed without waiting for an app restart."""
    create_attempt_id = CreateAttemptId()
    info = AgentCreateAttemptInfo(
        create_attempt_id=create_attempt_id,
        status=AgentCreateAttemptStatus.INITIALIZING,
        launch_mode=LaunchMode.DOCKER,
    )
    html = render_creating_page(create_attempt_id=create_attempt_id, info=info)
    assert 'id="create-attempt-dismiss-btn"' in html


def test_render_create_form_honors_workspace_env_vars_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the explicit opt-in, the MINDS_WORKSPACE_* env vars pre-fill the form.

    Used by ``just minds-start`` (and the e2e runner) to point the form at the
    operator's local DEFAULT_WORKSPACE_TEMPLATE worktree + current branch so the dev-iteration loop is
    one click.
    """
    monkeypatch.setenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", "1")
    monkeypatch.setenv("MINDS_WORKSPACE_GIT_URL", "/local/default_workspace_template/path")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "/local/default_workspace_template/path" in html
    assert "mngr/some-feature" in html


def test_render_create_form_honors_workspace_env_vars_on_staging_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-in is tier-independent: it works even on a shared tier (staging).

    Regression test: staging previously dropped MINDS_WORKSPACE_* unconditionally,
    so ``just minds-start`` against staging silently fell back to the public
    GitHub DEFAULT_WORKSPACE_TEMPLATE on ``main`` -- meaning local DEFAULT_WORKSPACE_TEMPLATE changes could never be tested
    against staging.
    """
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds-staging")
    monkeypatch.setenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", "1")
    monkeypatch.setenv("MINDS_WORKSPACE_GIT_URL", "/local/default_workspace_template/path")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "/local/default_workspace_template/path" in html
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
    monkeypatch.setenv("MINDS_WORKSPACE_GIT_URL", "/local/default_workspace_template/path")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "/local/default_workspace_template/path" not in html
    assert "mngr/some-feature" not in html
    # And the hardcoded git-URL fallback DOES appear (form is still usable).
    assert "default-workspace-template" in html


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


_INSPIRATION_URL = "https://github.com/acme/inspiration"


def _render_inspiration(**kwargs: Any) -> str:
    return render_inspiration_create_page(git_url=_INSPIRATION_URL, **kwargs)


def test_render_inspiration_page_shows_chooser_options() -> None:
    html = _render_inspiration()
    assert "You've opened an Inspiration" in html
    assert "Create a new machine" in html
    assert "Add to an existing machine" in html
    assert _INSPIRATION_URL in html
    # The eyebrow "INSPIRATION" label above the heading is gone.
    assert ">Inspiration</p>" not in html
    # Full-width (block) buttons press with a gentler scale than the base 0.98.
    assert "active:!scale-[0.99]" in html


def test_render_inspiration_page_add_flow_has_copyable_skill_message() -> None:
    # The skill accepts only a git URL, so the message must exclude the branch
    # even when the deeplink carried one.
    html = _render_inspiration(branch="v1.2.3")
    assert f"/use-inspiration {_INSPIRATION_URL}" in html
    assert f"/use-inspiration {_INSPIRATION_URL} v1.2.3" not in html
    assert 'id="inspiration-copy-btn"' in html
    # The whole box is clickable, not just the button.
    assert "copyBox.addEventListener('click', doCopy)" in html
    # The box has a hover animation (a .copy-box hover rule + the class on it).
    assert "copy-box" in html
    assert ".copy-box:hover" in html
    # The read-only value shows a pointer cursor (the whole box copies on click).
    assert ".copy-box input" in html
    # The Copy button is borderless (ghost variant), not the bordered secondary.
    copy_btn = re.search(r'<button[^>]*\bid="inspiration-copy-btn"[^>]*>', html)
    assert copy_btn is not None
    assert "border-transparent" in copy_btn.group(0)
    assert "border-default" not in copy_btn.group(0)


def test_render_inspiration_page_confirm_step_labels_account() -> None:
    # The account picker on the confirm step sits under an "Account" label laid
    # out like the "Creating from" block above it (uppercase label, value below).
    html = _render_inspiration()
    assert ">Account</p>" in html
    assert ">Creating from</p>" in html


def test_render_inspiration_page_step_circle_current_black_previous_gray() -> None:
    # The current step is a solid black bubble with a white number; previous
    # (completed) steps are a light-gray bubble with a dark-gray number, driven
    # by data-state. The bubble carries no border, and the title text follows:
    # black on the current step, gray on previous ones.
    html = _render_inspiration()
    assert "circle.setAttribute('data-state'" in html
    # Current step: black bubble (inverse surface) + white number.
    assert '[data-step-circle][data-state="active"]' in html
    assert "background-color: var(--c-surface-inverse)" in html
    assert "color: var(--c-text-inverse-primary)" in html
    # Previous steps: opaque light-gray bubble + dark-gray number.
    assert '[data-step-circle][data-state="done"]' in html
    assert "color-mix(in srgb, var(--c-text-primary) 10%, var(--c-surface-primary))" in html
    # No border (and no plain white fill) on the bubble.
    circle_cls = re.search(r'data-step-circle class="([^"]*)"', html)
    assert circle_cls is not None
    assert "border" not in circle_cls.group(1)
    assert "bg-surface-primary" not in circle_cls.group(1)
    # The title text is black on the current step, gray on previous ones.
    assert "title.classList.toggle('text-primary', !opts.done)" in html
    assert "title.classList.toggle('text-secondary', opts.done)" in html
    # Completed steps are marked with a simple green check (text-success).
    assert 'class="text-success shrink-0"' in html


def test_render_inspiration_page_connector_solid_with_dotted_more_stub() -> None:
    # Regular links between steps are SOLID; the current (last visible) step
    # instead gets a short 3-dot "more to come" stub -- but only when it isn't
    # the final step (activeStep < 3), so the last number reads as the end.
    html = _render_inspiration()
    assert 'data-step-connector class="absolute hidden"' in html
    assert "c.classList.add('top-4', 'bottom-0', 'step-connector-line')" in html
    # The solid line's color matches the completed circle fill exactly.
    assert ".step-connector-line" in html
    assert "c.classList.add('step-connector-dots')" in html
    assert "activeStep < 3" in html
    # The 3-dot stub itself is a short dotted segment.
    assert "radial-gradient(circle, var(--c-border-strong)" in html


def test_render_inspiration_page_lists_workspaces_with_liveness_gating() -> None:
    rows = [
        InspirationWorkspaceRow(agent_id="agent-aa", name="alpha", accent="#112233", liveness="RUNNING"),
        InspirationWorkspaceRow(agent_id="agent-bb", name="beta", accent="#445566", liveness="STOPPED"),
    ]
    html = _render_inspiration(mngr_forward_origin="https://localhost:8421", workspace_rows=rows)
    assert 'data-agent-id="agent-aa"' in html
    assert 'data-liveness="STOPPED"' in html
    assert 'data-default-href="https://localhost:8421/goto/agent-aa/"' in html
    # The stopped-mind detour mirrors the landing rows' recovery shortcut.
    assert "intent=restart" in html
    # Each workspace card carries a right-pointing chevron to signal selection
    # (the chevron-right glyph path).
    assert "M5.57617 3.57617" in html


def test_render_inspiration_page_empty_workspace_list_links_to_new_flow() -> None:
    html = _render_inspiration(workspace_rows=[])
    assert "You don't have any machines yet." in html
    assert 'id="inspiration-empty-to-new"' in html


def test_render_inspiration_page_new_flow_requires_trust_checkbox() -> None:
    html = _render_inspiration()
    assert 'id="inspiration-trust"' in html
    assert "I trust this Inspiration" in html
    assert "not been approved or verified by Imbue" in html
    # The submit handler gates on the checkbox before any POST.
    assert "trustCheckbox.checked" in html


def test_render_inspiration_page_create_button_gated_on_trust() -> None:
    # Create starts disabled/grayed and the acknowledgment starts red; both
    # flip when the box is checked (syncTrustGate).
    html = _render_inspiration()
    assert re.search(r'id="inspiration-submit"[^>]*\sdisabled', html) or re.search(
        r'\sdisabled[^>]*id="inspiration-submit"', html
    )
    assert re.search(r'id="inspiration-trust-title"[^>]*text-important', html)
    assert "submitBtn.disabled = !ok" in html


def test_render_inspiration_page_is_three_steps_with_advanced_on_confirm() -> None:
    # Advanced settings are NOT their own step: the create flow is 3 steps
    # (choose -> where -> confirm), and an "Advanced settings" dashed divider on
    # the confirm step reveals the selects in place.
    html = _render_inspiration(branch="v1.2.3")
    assert 'id="inspiration-step-create-3"' in html
    assert 'id="inspiration-step-create-4"' not in html
    assert 'id="inspiration-toggle-advanced"' in html
    assert "Advanced settings" in html
    # The toggle starts as a plain left label: the flanking dashed rules are
    # hidden until it opens, when JS reveals them (a la the create advanced <hr>).
    assert re.search(r"data-adv-rule[^>]*border-dashed[^>]*hidden", html)
    # Opening smoothly animates the morph into the centered divider.
    assert "animateAdvancedDivider()" in html
    # Opening pins the centered column (no upward jump) then scrolls the options
    # into view, rather than letting the layout re-center upward.
    assert "pinPageColumn()" in html
    assert "revealAdvanced()" in html
    # The auto-scroll is a slow, duration-controlled scroll (not native smooth).
    assert "ADV_SCROLL_MS" in html
    # Closing plays the open in reverse (divider morph back) and collapses the
    # panel's height so the Create button rises as the options disappear.
    assert "closeAdvanced()" in html
    assert "reverseAdvancedDivider()" in html
    assert "collapseAdvancedPanel(" in html
    # The toggle shows a pointer cursor (it's a native button, which wouldn't).
    assert re.search(r'id="inspiration-toggle-advanced"[^>]*cursor-pointer', html)
    # The removed settings-choice step's controls are gone.
    assert 'id="inspiration-use-defaults"' not in html
    assert 'id="inspiration-configure-more"' not in html


def test_render_inspiration_page_advanced_panel_has_provider_and_region_selects() -> None:
    # The "Advanced settings" dropdown box reveals compute / backup provider and
    # region selects in place (no navigation away). Submit reads them. (AI
    # provider was removed from create, so it's absent here too.)
    html = _render_inspiration(
        region_options_by_launch_mode={"IMBUE_CLOUD": ["us-west", "eu"]},
        region_selected_by_launch_mode={"IMBUE_CLOUD": "us-west"},
    )
    for select_id in (
        "inspiration-launch-mode",
        "inspiration-backup-provider",
        "inspiration-region",
    ):
        assert f'id="{select_id}"' in html, select_id
    assert 'id="inspiration-ai-provider"' not in html
    assert "launch_mode: launchSelect.value" in html
    assert "region: regionSelect.disabled" in html
    # No navigation off this page -- the settings stay in the flow.
    assert "window.location = '/create?'" not in html


def test_render_inspiration_page_submit_labeled_create_from_inspiration() -> None:
    html = _render_inspiration()
    assert "Create from Inspiration" in html
    assert 'id="inspiration-submit"' in html


def test_render_inspiration_page_repo_shown_as_plain_text() -> None:
    # The repo is display-only and deliberately not rendered as an input-looking
    # box: it appears as plain paragraph text, and the POSTed value rides in a
    # hidden input. There is no editable git_url form field.
    html = _render_inspiration()
    assert 'name="git_url"' not in html
    assert 'id="inspiration-git-url"' in html
    assert f">{_INSPIRATION_URL}</p>" in html


def test_render_inspiration_page_downstream_steps_start_hidden() -> None:
    # Progressive disclosure: only step 1 is visible on load; every downstream
    # step wrapper starts hidden and is revealed by the timeline's JS. The
    # ``hidden`` toggle must not sit on a ``flex`` element (flex would win), so
    # it lives on the plain step wrapper.
    html = _render_inspiration()
    assert re.search(r'id="inspiration-step-1" class="inspiration-step"', html)
    for step_id in (
        "inspiration-step-create-2",
        "inspiration-step-create-3",
        "inspiration-step-add-2",
        "inspiration-step-add-3",
    ):
        assert re.search(rf'id="{step_id}" class="inspiration-step hidden"', html), step_id


def test_render_inspiration_page_gates_each_step_on_the_previous() -> None:
    # The stepper shows one step's body at a time: picking a pathway advances
    # to step 2, and copying the message / choosing a preset advances to step
    # 3. render() hides every non-active step's body.
    html = _render_inspiration()
    assert "function render()" in html
    assert "isMessageCopied = true" in html
    assert "activeStep = 3" in html
    assert "chooseBranch('create')" in html
    assert "chooseBranch('add')" in html
    # Completed steps collapse to a summary of the choice instead of the options.
    assert "data-step-summary" in html
    assert "'Create a new machine'" in html


def test_render_inspiration_page_has_animations_and_copy_feedback() -> None:
    # Each newly-shown step/answer plays one self-contained opacity+slide
    # reveal to a fixed position (no competing reflow); completed circles pop;
    # the Copy box turns green (theme success variable) before advancing.
    html = _render_inspiration()
    assert "animateReveal" in html
    assert "animatePop" in html
    assert "var(--c-success)" in html
    assert "'Copied'" in html


def test_render_inspiration_page_number_and_title_go_back() -> None:
    # Both the step number and its title are click-to-change affordances.
    html = _render_inspiration()
    assert "function wireGoBack" in html
    assert "[data-step-circle]" in html
    assert "[data-step-title]" in html


def test_render_inspiration_page_skill_message_has_stable_id() -> None:
    # The copy handler reads the message by id, so the CopyField must carry it.
    html = _render_inspiration()
    assert 'id="inspiration-skill-message"' in html


def test_render_inspiration_page_carries_branch_hidden_input() -> None:
    html = _render_inspiration(branch="v1.2.3")
    assert 'id="inspiration-branch" value="v1.2.3"' in html
    blank = _render_inspiration()
    assert 'id="inspiration-branch" value=""' in blank


def test_render_inspiration_page_opens_signin_modal_via_overlay_bridge() -> None:
    html = _render_inspiration(accounts=[])
    assert "window.minds.openSigninModal()" in html
    assert "/auth/signin-modal" in html


def test_render_inspiration_page_presets_match_create_form() -> None:
    # Same two presets and the same provider values as the create form's
    # PRESETS map, so both pages create identically-configured workspaces.
    html = _render_inspiration()
    assert 'data-preset="remote"' in html
    assert 'data-preset="local"' in html
    for value in ("IMBUE_CLOUD", "LIMA", "CONFIGURE_LATER"):
        assert value in html


def test_resolve_create_host_name_uses_submitted_value() -> None:
    assert str(resolve_create_host_name("my-workspace")) == "my-workspace"


def test_resolve_create_host_name_generates_workspace_name_when_empty() -> None:
    # No submitted name and no existing workspaces -> the first ``workspace-N`` name.
    assert str(resolve_create_host_name("")) == "workspace-1"


def test_resolve_create_host_name_picks_next_free_workspace_name() -> None:
    # The fallback skips names already in use across providers.
    assert str(resolve_create_host_name("", {"workspace-1", "workspace-2"})) == "workspace-3"


def test_make_unique_host_name_numbered_empty_is_one() -> None:
    assert str(make_unique_host_name("mind", set(), always_number=True)) == "mind-1"


def test_make_unique_host_name_numbered_increments_past_used() -> None:
    assert str(make_unique_host_name("mind", {"mind-1", "mind-2", "mind-3"}, always_number=True)) == "mind-4"


def test_make_unique_host_name_numbered_reuses_lowest_gap() -> None:
    # A destroyed ``mind-2`` leaves a gap that is filled before climbing higher.
    assert str(make_unique_host_name("mind", {"mind-1", "mind-3"}, always_number=True)) == "mind-2"


def test_make_unique_host_name_numbered_ignores_non_canonical_suffixes() -> None:
    # Names that merely start with ``mind-`` but are not a canonical positive
    # integer (a coolname, a zero-padded number, ``mind-0``) do not take the
    # ``mind-1`` slot, and unrelated names are ignored entirely.
    existing = {"mind-foo", "mind-01", "mind-0", "brave-cool-otter", "mindful"}
    assert str(make_unique_host_name("mind", existing, always_number=True)) == "mind-1"


def test_make_unique_host_name_bare_when_free() -> None:
    assert str(make_unique_host_name("mindtest", set())) == "mindtest"
    assert str(make_unique_host_name("mindtest", {"other"})) == "mindtest"


def test_make_unique_host_name_bare_then_numbered_from_two() -> None:
    # When the bare base is taken, suffixes start at 2 (so the bare name reads
    # as the "first").
    assert str(make_unique_host_name("mindtest", {"mindtest"})) == "mindtest-2"
    assert str(make_unique_host_name("mindtest", {"mindtest", "mindtest-2"})) == "mindtest-3"


def test_render_login_page_shows_prompt() -> None:
    html = render_login_page()
    assert "login URL" in html.lower() or "Login" in html


def test_render_chrome_page_contains_titlebar() -> None:
    html = render_chrome_page()
    assert "minds-titlebar" in html
    assert "home-btn" in html
    assert "back-btn" in html
    assert "content-frame" in html
    # The home button reads "(icon) Minds"; there is no hamburger menu, no
    # forward arrow, and no centered page title.
    assert ">Minds</span>" in html
    assert "sidebar-toggle" not in html
    assert "forward-btn" not in html
    assert 'id="page-title"' not in html


def test_render_chrome_page_contains_workspace_crumb_and_icon_tabs() -> None:
    # The breadcrumb block ("/ workspace-name (chevron)") and the two
    # workspace icon-tabs render hidden; chrome.js shows them on
    # workspace-scoped screens. The switcher button anchors the workspace
    # menu beneath itself.
    html = render_chrome_page()
    assert 'id="ws-crumb"' in html
    assert 'id="workspace-switcher-btn"' in html
    assert 'id="ws-tab-strip"' in html
    assert 'id="ws-tab-share"' in html
    assert 'id="ws-tab-settings"' in html
    # The Workspace icon-tab was removed: in Electron the workspace is simply
    # the view behind the options panel, so a tab pointing at it was a no-op.
    assert 'id="ws-tab-workspace"' not in html
    # The Connections icon-tab was removed; pending permission requests are
    # served by the titlebar's inbox popup instead.
    assert 'id="ws-tab-connections"' not in html
    assert 'id="page-crumb"' in html
    # Visibility is driven through the native ``hidden`` attribute (the blocks
    # carry flex display classes that would beat a ``hidden`` class).
    assert 'id="ws-crumb" class="flex items-center min-w-0" hidden' in html


def test_render_chrome_page_seeds_workspace_crumb_server_side() -> None:
    # The desktop shell passes the workspace being loaded (?agent=... resolved
    # to a name by the route) so the wrapper's first paint already shows the
    # workspace breadcrumb -- no bare "Minds" bar while the content view
    # loads. The chrome wrapper is not itself one of the options panes, so
    # neither icon-tab is marked active. Without a crumb the block renders
    # hidden exactly as before.
    html = render_chrome_page(crumb_workspace_name="my-mind", crumb_agent_id="agent-abc123")
    assert 'id="ws-crumb" class="flex items-center min-w-0">' in html
    assert 'data-agent-id="agent-abc123"' in html
    assert ">my-mind</span>" in html
    assert 'id="ws-tab-share"' in html
    bare = render_chrome_page()
    assert 'id="ws-crumb" class="flex items-center min-w-0" hidden' in bare


def test_render_chrome_page_contextual_back_button_starts_hidden() -> None:
    # The back arrow is contextual: hidden at rest, shown by chrome.js only on
    # pages that opt in (e.g. the create form). There is no forward arrow.
    html = render_chrome_page()
    back_open = html.index('id="back-btn"')
    back_tag = html[html.rindex("<button", 0, back_open) : html.index(">", back_open)]
    assert " hidden" in back_tag


def test_render_chrome_page_titlebar_is_left_cluster_plus_right_cluster() -> None:
    # The titlebar is a growing left cluster (breadcrumb + icon-tabs) and a
    # shrink-0 right cluster (bug report + non-mac window controls); there is
    # no centered title section.
    html = render_chrome_page()
    titlebar = html[html.index('id="minds-titlebar"') : html.index('id="sidebar-backdrop"')]
    assert titlebar.count("flex-1") == 1
    assert "flex-[2]" not in titlebar
    assert "justify-end shrink-0" in titlebar


def test_render_chrome_page_titlebar_reserves_mac_traffic_lights_with_spacer() -> None:
    # On macOS the traffic-light strip is reserved with a fixed shrink-0 spacer
    # div *inside* the left flex-1 section -- NOT a left padding. With
    # box-sizing: border-box a left padding clamps the section's flex base size
    # up to the padding, making the equal-width left section wider than the
    # right and shoving the centered title ~36px off-center; a spacer instead
    # lives inside the section (which min-w-0 lets shrink to its flex share), so
    # both sides stay equal width and the title stays truly centered. Non-mac
    # has no such reservation (it draws its own controls on the right instead).
    html_mac = render_chrome_page(is_mac=True)
    html_other = render_chrome_page(is_mac=False)
    # The padding approach is the bug being fixed: it must not come back.
    assert "pl-[72px]" not in html_mac
    assert "pl-[72px]" not in html_other
    # The spacer sits at the very start of the left section, ahead of the back
    # button (#back-btn), only on macOS.
    left_section_mac = html_mac[: html_mac.index('id="back-btn"')]
    assert 'class="w-[72px] shrink-0" aria-hidden="true"' in left_section_mac
    assert "w-[72px]" not in html_other


def test_render_chrome_page_requests_badge_is_inline_count() -> None:
    # The titlebar's inbox button (right cluster) carries the pending-request
    # badge: the Badge count pill sat inline beside the inbox icon (gap-[3px]
    # row), not a dot overlapping the icon's corner. It carries the type-badge
    # pill role and no absolute positioning (chrome.js fills the count text +
    # toggles the native `hidden` attribute from the global SSE requests count).
    html = render_chrome_page()
    assert 'id="requests-toggle"' in html
    assert 'id="requests-badge"' in html
    assert "type-badge" in html
    assert "gap-[3px]" in html
    # No corner overlay: the badge no longer pins itself to the top-right.
    assert "top-0.5 right-0.5" not in html
    # Hidden at rest via the native `hidden` ATTRIBUTE, not a `hidden` class: the
    # pill bakes in `inline-flex`, which beats the `.hidden` utility, so a class
    # would leave a stray "0" showing. Match the bare attribute on the pill.
    assert 'id="requests-badge" hidden>' in html
    assert 'id="requests-badge" class="hidden"' not in html


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


def test_render_chrome_page_crumbs_use_type_label_tokens() -> None:
    # The breadcrumb text (workspace name / page name) uses plain type-label +
    # text tokens; the ``.titlebar-surface`` scope re-bases those tokens off
    # --titlebar-bg, so the crumbs flip black/white with the accent's
    # lightness (in pure CSS).
    html = render_chrome_page()
    assert 'id="workspace-switcher-name" class="type-label' in html
    assert 'id="page-crumb-name" class="type-label text-primary' in html


def test_render_chrome_page_switcher_menu_has_only_new_workspace() -> None:
    # The titlebar carries no account button (``id="user-btn"``). The floating
    # switcher menu's bottom section was trimmed to just the "New workspace"
    # CTA: the "Minds Settings" and "Manage account(s)" / "Log in" entries were
    # removed (Minds Settings is still reachable from the home screen).
    html = render_chrome_page()
    assert 'id="user-btn"' not in html
    assert 'id="sidebar-new-workspace"' in html
    assert 'id="sidebar-settings"' not in html
    assert 'id="sidebar-account"' not in html


def test_render_chrome_page_content_iframe_uses_12px_rounded_corners() -> None:
    # 12px radius (``rounded-[12px]``) matches Electron-side
    # ``contentView.setBorderRadius(12)`` (= ``CONTENT_CORNER_RADIUS`` in
    # electron/main.js) so both modes render the same tucked-under shape
    # against the OS's outer window rounding. It is a structural exception to
    # the 4-step radius scale (4/6/8/16) -- pinned as an arbitrary value so it
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


def test_edge_to_edge_surfaces_opt_out_of_scrollbar_gutter() -> None:
    """Regression: with classic (always-visible) scrollbars on macOS, the
    global ``html { scrollbar-gutter: stable }`` rule reserved a 15px gutter
    on the edge-to-edge chrome/overlay surfaces that nothing painted, so
    tooltips were clipped mid-label and modal dim backdrops stopped short of
    the window's right edge. Those surfaces must opt out via the
    ``no-scrollbar-gutter`` class on the html element, and app.css must
    define the opt-out rule."""
    css = _TOKENS_CSS_PATH.read_text()
    assert "html.no-scrollbar-gutter" in css
    opted_out = '<html lang="en" class="no-scrollbar-gutter">'
    # Every ChromeShell surface is a viewport-locked, edge-to-edge document (the
    # document lock is a universal ``html, body`` rule in app.css), so all of
    # them opt out of the document scrollbar gutter -- the titlebar spans the
    # full window width and its buttons never shift between pages.
    assert opted_out in render_chrome_page()
    assert opted_out in render_overlay_host_page()
    assert opted_out in render_sidebar_page()
    assert opted_out in render_help_page(workspace_agent_id="")
    assert opted_out in render_inbox_page(cards=())
    # Local pages scroll inside the local-page-scroll card (whose own stable
    # gutter absorbs classic-scrollbar layout shifts) rather than the document.
    assert opted_out in render_landing_page(accessible_agent_ids=())
    assert 'id="local-page-scroll"' in render_landing_page(accessible_agent_ids=())
    # The document viewport-lock lives in app.css (moved out of the template
    # <style>), applied universally so no per-page class is needed.
    assert "overflow: hidden" in css


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
    # Chrome.jinja (browser mode) and Sidebar.jinja (the switcher page loaded
    # into the shared modal WebContentsView in Electron). It now carries only
    # the "New workspace" CTA; the "Minds Settings" and "Manage account(s)" /
    # "Log in" entries were removed.
    assert 'id="sidebar-new-workspace"' in html
    assert 'id="sidebar-settings"' not in html
    assert 'id="sidebar-account"' not in html


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
    # nudged 24px left (offset_x=-24 -> 0 + -24) and 2px below it
    # (offset_y=2 -> 0 + 38 + 2) -- right shape for "open the sidebar from
    # the first titlebar button" without any caller customization. The -24
    # lines a row's workspace-name label up under the breadcrumb's name text.
    html_default = render_sidebar_page()
    assert "left:-24px" in html_default
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
    # The versioned workspace surface the page's JS drives.
    assert "/api/v1/workspaces/" in html
    # The only restart the recovery page dispatches (a ``scope`` body on the
    # versioned restart route) plus the health probe it calls on load.
    assert "/restart" in html
    assert "scope: 'host'" in html
    assert "/health" in html
    assert 'data-initial-status="stuck"' in html


def test_render_recovery_page_restarting_status() -> None:
    html = render_recovery_page(
        agent_id=_AGENT_B,
        return_to="",
        initial_status="restarting",
        initial_error="",
    )
    assert 'data-initial-status="restarting"' in html


def test_render_recovery_page_restarting_copy_reflects_restart_flavor() -> None:
    """The RESTARTING branch names a full manual bounce but stays neutral for a start-only dispatch.

    A reload during an in-flight restart lands in the RESTARTING branch. A full
    manual bounce (the right-click "Restart machine", which POSTs the restart
    and then navigates here fresh) is a known restart, so the page reads
    "Restarting your machine"; the page's own start-only entry dispatch may be
    a no-op, so it stays on the neutral "Loading machine" spinner. The offline
    hint wins over both. Regression: the branch previously rendered the neutral
    spinner for every non-offline restart, so a deliberate right-click restart
    showed only "Loading machine".
    """
    full_html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="restarting",
        initial_error="",
        restart_is_start_only=False,
    )
    start_only_html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="restarting",
        initial_error="",
        restart_is_start_only=True,
    )
    # The flavor rides to the client as a data attribute the branch reads.
    assert 'data-restart-start-only="0"' in full_html
    assert 'data-restart-start-only="1"' in start_only_html
    # The RESTARTING branch selects the copy off the flavor, with the offline
    # hint taking precedence over both.
    entry = full_html[full_html.rfind("if (initialStatus === 'restarting')") :]
    restarting_branch = entry[: entry.find("else if")]
    assert "restartStartOnly ? renderLoading : renderRestarting" in restarting_branch
    assert "hostOffline" in restarting_branch


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
    for tier in (
        "'healthy'",
        "'backend_unreachable'",
        "'indeterminate'",
    ):
        assert tier in html, f"recovery page JS missing branch for {tier}"
    # The interface_unresponsive tier (and its surgical in-place restart) is
    # gone: the server never emits it. The concrete verdicts (host_offline,
    # host_unresponsive, unknown tiers) share the catch-all consent-page
    # branch -- tiers are display-only, so no per-verdict dispatch exists.
    assert "interface_unresponsive" not in html
    assert "scope: 'services'" not in html
    # The shared landing places for each branch.
    assert "renderUnresponsive" in html
    assert "renderBackendUnreachable" in html
    assert "renderReconnecting" in html


def test_render_recovery_page_fresh_entry_dispatches_start_only_unconditionally() -> None:
    """A fresh (stuck) entry dispatches the start-only restart immediately, with no probe gate.

    The dispatch decision no longer consults any host-state knowledge: the
    start-only restart is safe regardless of the host's state (``mngr start``
    checks ground truth at commit time and no-ops on a live host), so the entry
    fires it unconditionally and the classifier tiers stay display-only. The
    in-flight copy claims only what is known (the offline copy off the hint,
    else the neutral loading spinner -- never "Restarting your machine"),
    and applyHealth -- the display path -- must contain no dispatch at all.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    # The fresh entry (the trailing else of the initialStatus dispatcher) POSTs
    # the start-only restart directly.
    entry = html[html.rfind("if (initialStatus === 'restarting')") :]
    assert "postRestart(" in entry
    assert "{ scope: 'host', start_only: true }" in entry
    # No probe-gated dispatch remains: applyHealth renders, never restarts.
    apply_start = html.find("function applyHealth(")
    apply_block = html[apply_start : html.find("function ", apply_start + 1)]
    assert "postRestart" not in apply_block
    # postRestart renders a pending state while the dispatch is in flight;
    # the default (renderRestarting) is reserved for the manual click, where
    # the restart is known.
    post_start = html.find("function postRestart(")
    post_block = html[post_start : html.find("function ", post_start + 1)]
    assert "(renderPending || renderRestarting)()" in post_block
    # The entry dispatch claims only what it knows: the offline copy when the
    # host reads offline, else the neutral loading spinner -- never
    # "Restarting your workspace", since the start may be a no-op.
    assert "hostOffline ? renderRestartingOffline : renderLoading" in entry
    # A page load that lands on the RESTARTING tracker state picks its copy from
    # the restart's flavor: a start-only dispatch stays on the neutral loading
    # spinner, a full manual bounce names the restart. The offline hint wins over
    # both (a cold boot reads as the offline revival copy).
    restarting_entry = entry[: entry.find("else if")]
    assert "restartStartOnly ? renderLoading : renderRestarting" in restarting_entry
    assert "hostOffline" in restarting_entry


def test_render_recovery_page_offline_copy_is_display_only() -> None:
    """The offline restarting copy is selected by the render hint and upgraded by the poll header.

    The entry dispatch picks ``renderRestartingOffline`` ("Bringing your
    workspace back online") when the render-time ``data-host-offline`` hint
    reads 1. When the hint was stale (a cold launch still replaying a
    pre-stop RUNNING), the convergence poll's ``X-Workspace-Offline`` header
    upgrades the copy one-way once discovery lands the STOPPED observation --
    and only for the start-only entry dispatch, so a manual bounce's transient
    STOPPED never rewrites the page as an offline revival. Display-only:
    ``applyHealth`` stays dispatch-free regardless of the hint.
    """
    offline_html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
        initial_offline=True,
    )
    assert 'data-host-offline="1"' in offline_html
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    assert 'data-host-offline="0"' in html
    # The offline render names the offline condition.
    offline_start = html.find("function renderRestartingOffline")
    offline_block = html[offline_start : html.find("function ", offline_start + 1)]
    assert "was offline" in offline_block
    # The entry dispatch picks the render off the hint...
    entry = html[html.rfind("if (initialStatus === 'restarting')") :]
    assert "hostOffline ? renderRestartingOffline : renderLoading" in entry
    # ...and the convergence poll upgrades it off the per-tick header, gated on
    # the dispatch having been the start-only one.
    refresh_start = html.find("function scheduleRefresh")
    refresh_block = html[refresh_start : html.find("function scheduleHealthyPoll")]
    assert "maybeUpgradeToOfflineCopy(resp)" in refresh_block
    upgrade_start = html.find("function maybeUpgradeToOfflineCopy")
    upgrade_block = html[upgrade_start : html.find("function ", upgrade_start + 1)]
    assert "X-Workspace-Offline" in upgrade_block
    assert "startOnlyDispatched" in upgrade_block


def test_render_recovery_page_indeterminate_renders_reconnecting_not_a_verdict() -> None:
    """The INDETERMINATE tier keeps checking instead of rendering a verdict.

    When the probe timed out or the snapshot is stale, the page must not show a
    restart verdict off non-evidence -- it renders the live "reconnecting"
    state and re-probes slowly. The branch must come before the catch-all
    verdict branch so an indeterminate result keeps checking rather than
    rendering the "Machine unresponsive" verdict.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    apply_start = html.find("function applyHealth(")
    apply_block = html[apply_start : html.find("function ", apply_start + 1)]
    assert "'indeterminate'" in apply_block
    assert "renderReconnecting()" in apply_block
    assert "scheduleIndeterminateReprobe()" in apply_block
    assert apply_block.find("'indeterminate'") < apply_block.rfind("renderUnresponsive()")
    # renderReconnecting shows a spinner and no restart button, and arms the poll.
    recon_start = html.find("function renderReconnecting")
    recon_block = html[recon_start : html.find("function ", recon_start + 1)]
    assert "show(hostBtn, false)" in recon_block
    assert "armHealthyPoll()" in recon_block


def test_render_recovery_page_dropped_probe_request_reconnects_not_a_verdict() -> None:
    """A probe request that fails outright must reconnect-and-retry, not dead-end.

    This is the post-macOS-sleep strand: Chromium aborts the in-flight health
    fetch when the machine suspends, so ``fetchHealth`` rejects. The old handler
    rendered the terminal "Machine unresponsive" verdict and never re-probed,
    stranding the user even after the machine came back. The rejection handler
    must instead render the live "reconnecting" state and schedule a retry, so
    the cheap liveness poll returns the user home and the slow re-probe
    converges to a real tier.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="https://example.test/workspace",
        initial_status="stuck",
        initial_error="",
    )
    # runProbe contains an inline ``function (data)`` callback, so slice to the
    # next top-level statement (the hostBtn click handler) rather than the next
    # ``function `` token.
    probe_start = html.find("function runProbe(")
    probe_block = html[probe_start : html.find("hostBtn.addEventListener", probe_start)]
    # The success path still applies the health payload...
    assert "applyHealth(data)" in probe_block
    # ...and the rejection path reconnects + retries instead of a static verdict.
    assert "renderReconnecting()" in probe_block
    assert "scheduleIndeterminateReprobe()" in probe_block
    assert "renderUnresponsive()" not in probe_block


def test_render_recovery_page_every_wait_state_arms_the_homeward_poll() -> None:
    """No recovery state is a dead end: each waiting state arms the cheap liveness poll.

    This is the fix for the post-macOS-sleep "Machine unresponsive" strand: a
    machine that comes back on its own must return the user home without any
    action. Every terminal/waiting render arms the poll, and the stuck entry
    arms it before dispatching the start-only restart, so a machine that
    answers while the dispatch settles still goes straight home.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    for fn in ("renderUnresponsive", "renderDispatchError", "renderReconnecting", "renderBackendUnreachable"):
        start = html.find("function " + fn)
        block = html[start : html.find("function ", start + 1)]
        assert "armHealthyPoll()" in block, f"{fn} must arm the homeward poll so it is not a dead end"
    # The stuck entry arms the poll before dispatching the start-only restart.
    entry = html[html.rfind("if (initialStatus === 'restarting')") :]
    assert 0 < entry.find("armHealthyPoll();") < entry.rfind("postRestart(")


def test_render_recovery_page_backend_unreachable_offers_retry_not_restart() -> None:
    """The backend-unreachable state must surface a Retry affordance and a background
    healthy-poll (auto-return on recovery), and must NOT auto-dispatch or offer a host
    restart (a restart routes through the unreachable backend, so it cannot help).
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    assert 'id="recovery-retry-btn"' in html
    # The backend render shows the Retry and the "Can't connect to" copy; it
    # must not fall through to a restart dispatch.
    provider_start = html.find("function renderBackendUnreachable")
    assert provider_start >= 0
    provider_end = html.find("function ", provider_start + 1)
    provider_block = html[provider_start:provider_end]
    assert "Can't connect to" in provider_block
    assert "show(retryBtn, true)" in provider_block
    assert "postRestart" not in provider_block
    # The copy must be provider-agnostic: a local docker daemon is independent of
    # the network, so the old "check your internet connection" line is wrong here
    # and must not return.
    assert "internet connection" not in provider_block.lower()
    # Instead of a hand-authored per-provider message, the verbatim provider
    # error rides along on the response (``unreachable_reason``) and is surfaced.
    assert "unreachable_reason" in provider_block
    assert "providerReasonEl.textContent = reason" in provider_block
    # Diagnostics are suppressed on this tier (the cause is the external backend,
    # shown verbatim, not anything the in-container probes inspect).
    assert "show(debugDetailsEl, false)" in provider_block
    # The render arms the cheap liveness poll so the page auto-returns the user
    # once the backend recovers and the tracker flips HEALTHY.
    assert "armHealthyPoll()" in provider_block
    # The display path contains no dispatch at all (tiers are display-only).
    apply_start = html.find("function applyHealth(")
    apply_block = html[apply_start : html.find("function ", apply_start + 1)]
    assert "postRestart" not in apply_block
    # The verdict must stay live: a transient provider error (one failed
    # discovery cycle, e.g. during app startup) is cleared by the provider's
    # next clean snapshot, so the branch schedules the slow re-probe to
    # re-classify and continue the flow instead of dead-ending on the page.
    unreachable_branch = apply_block[apply_block.find("'backend_unreachable'") : apply_block.find("'healthy'")]
    assert "scheduleIndeterminateReprobe()" in unreachable_branch
    # Only one reprobe timer may be pending at once: the Retry button's
    # immediate probe re-enters applyHealth, which would otherwise spawn a
    # parallel self-perpetuating probe chain per click.
    reprobe_start = html.find("function scheduleIndeterminateReprobe")
    reprobe_block = html[reprobe_start : html.find("function ", reprobe_start + 1)]
    assert "if (reprobePending || restartDispatched) return" in reprobe_block


def test_render_recovery_page_unresponsive_verdict_stays_live_and_resets_state() -> None:
    """The unresponsive verdict is not a dead-end, and verdict renders reset each other's elements.

    The catch-all verdict branch (host_offline / host_unresponsive / unknown
    tiers) must keep re-probing so the failure page's verdict and diagnostics
    stay live as evidence changes. And because the page can move between
    verdicts (backend_unreachable -> host_unresponsive), renderUnresponsive and
    renderDispatchError must hide the Retry button and clear the provider-error
    paragraph that renderBackendUnreachable showed -- otherwise the page shows
    Retry AND Restart together with a stale provider error.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    apply_start = html.find("function applyHealth(")
    apply_block = html[apply_start : html.find("function ", apply_start + 1)]
    # The catch-all verdict branch (after the indeterminate branch) renders the
    # consent page and schedules the slow re-probe.
    fallthrough = apply_block[apply_block.rfind("renderUnresponsive()") :]
    assert "scheduleIndeterminateReprobe()" in fallthrough
    for fn in ("renderUnresponsive", "renderDispatchError"):
        start = html.find("function " + fn)
        block = html[start : html.find("function ", start + 1)]
        assert "show(retryBtn, false)" in block, f"{fn} must hide the backend-unreachable Retry button"
        assert "providerReasonEl.textContent = ''" in block, f"{fn} must clear the provider error text"


def test_render_recovery_page_restart_dispatch_silences_reprobe_chain() -> None:
    """A dispatched restart must silence the reprobe chain the verdict left armed.

    The unresponsive verdict shows the Restart button while its slow re-probe
    chain stays perpetually armed (a pending timer, or a heavy probe already in
    flight), so a manual restart always races a stale probe result. Without a
    guard that result overwrites the "Restarting your machine" render (and
    can re-POST a restart) seconds after the click, for the whole restart
    duration. postRestart flips restartDispatched; applyHealth drops results
    that arrive after it, and scheduleIndeterminateReprobe stops arming (and
    firing) timers.
    """
    html = render_recovery_page(
        agent_id=_AGENT_A,
        return_to="",
        initial_status="stuck",
        initial_error="",
    )
    post_start = html.find("function postRestart(")
    post_block = html[post_start : html.find("function ", post_start + 1)]
    assert "restartDispatched = true" in post_block
    apply_start = html.find("function applyHealth(")
    apply_block = html[apply_start : html.find("function ", apply_start + 1)]
    assert "if (restartDispatched) return" in apply_block
    reprobe_start = html.find("function scheduleIndeterminateReprobe")
    reprobe_block = html[reprobe_start : html.find("function ", reprobe_start + 1)]
    assert "if (reprobePending || restartDispatched) return" in reprobe_block


def test_render_recovery_page_loading_hides_diagnostic_dropdown() -> None:
    """renderLoading must hide the diagnostic dropdown so a stale prior diagnostic
    does not linger on the page while a fresh check is in flight (issue: user
    clicked Restart machine and the previous probe's diagnostic stayed open).
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
    # The restart_failed branch in the dispatcher calls runProbe() so the
    # diagnostics are populated (the probe path is display-only, so this can
    # never dispatch another restart).
    assert "restart_failed" in html
    assert "runProbe()" in html
    # The error-details DOM hook is rendered alongside the diagnostic.
    assert 'id="recovery-error"' in html
    assert 'id="recovery-debug-details"' in html


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
    (``--color-*``). The raw value layer (``--c-*``) and the runtime-set chrome
    variables (``--workspace-accent`` / ``--titlebar-*``) are implementation
    detail behind the tokens and are intentionally NOT surfaced.
    """
    css = _TOKENS_CSS_PATH.read_text()
    # ``--color-*: ...`` declarations only (the @theme token layer); the
    # border-compat shim's ``var(--color-gray-200, ...)`` is a reference, not a
    # declaration, so it is not matched.
    declared = set(re.findall(r"(--color-[a-z0-9-]+)\s*:", css))

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
    # Each variant contributes a defining class: solid variants a fill,
    # secondary its border (it has no resting fill), ghost its transparent base.
    variants_to_class = {
        "primary": "bg-surface-inverse",
        "secondary": "border-default",
        "danger": "bg-important",
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
    # md size = px-4 py-2 rounded-md type-label (Figma default: 16px / 8px padding)
    assert "px-4" in html
    assert "py-2" in html
    assert "rounded-md" in html
    assert "type-label" in html
    # Should not pick up lg-specific geometry
    assert "py-3" not in html
    assert "rounded-lg" not in html


def test_button_size_lg_uses_block_cta_geometry() -> None:
    html = CATALOG.render("Button", variant="primary", size="lg", block=True, _content="Sign in")
    assert "py-3" in html
    # All button sizes share the md control radius (6px).
    assert "rounded-md" in html
    assert "type-label" in html
    assert "w-full" in html


def test_button_size_icon_uses_square_padding() -> None:
    html = CATALOG.render("Button", variant="ghost", size="icon", _content="<svg/>")
    assert "p-1.5" in html
    # No horizontal/vertical padding mismatch (only one padding utility)
    assert "px-3" not in html
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
        _attrs={"title": "Restart", "aria-label": "Restart machine", "data-x": "y"},
    )
    assert 'title="Restart"' in html
    assert 'aria-label="Restart machine"' in html
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
    # nav variant => square padded icon button (p-1.5 rounded-md, no fixed w/h);
    # default tone => always text-primary + hover:bg-fill-hover, re-based
    # per-workspace by the .titlebar-surface scope in app.css.
    assert "p-1.5" in html
    assert "rounded-md" in html
    assert "text-primary" in html
    assert "text-secondary" not in html
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
    # The shared foreground token still applies (always text-primary).
    assert "text-primary" in html


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


_DESKTOP_CLIENT_DIR = Path(_templates_module.__file__).resolve().parent
_BUTTON_BUSY_SCRIPT = "/_static/button_busy.js"


def test_every_template_loading_a_button_busy_user_loads_the_helper_first() -> None:
    """A script that calls window.mindsButtonBusy is dead without button_busy.js beside it.

    The failure is silent and looks like the button doing nothing: the click
    handler throws on the missing global, so the request is never sent and the
    button never reports a wait. Checked across the whole templates tree rather
    than on the pages that happen to use it today, so a new surface that adopts
    one of these scripts cannot forget the helper.
    """
    static_dir = _DESKTOP_CLIENT_DIR / "static"
    users = sorted(
        path.name
        for path in static_dir.glob("*.js")
        # The helper defines the global; only its callers need it loaded first.
        if path.name != "button_busy.js" and "window.mindsButtonBusy" in path.read_text()
    )
    assert users, "no script uses the helper; this guard is watching nothing"

    helper_tag = f'src="{_BUTTON_BUSY_SCRIPT}"'
    for template in sorted((_DESKTOP_CLIENT_DIR / "templates").rglob("*.jinja")):
        source = template.read_text()
        for user in users:
            # Match the script tag, not any mention: components name the script
            # that drives them in their docstrings without loading it.
            tag = f'src="/_static/{user}"'
            if tag not in source:
                continue
            assert helper_tag in source, f"{template.name} loads {user} without the busy helper"
            # Both are ``defer``, so document order is execution order.
            assert source.index(helper_tag) < source.index(tag), f"{template.name} loads {user} before the busy helper"


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
    """The SHA-derived OKLCH accent system is gone: machine accents are
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


# -- Design-system scale guards --
#
# We keep Tailwind's stock spacing scale (--spacing is the default 0.25rem, so
# p-1 = 4px, p-4 = 16px) but constrain padding / margin / gap to a fixed subset
# of the native steps; radius is constrained to four named steps. These guards
# scan the authored source (templates / static / templates.py, never the
# generated app.min.css) and fail if an off-scale value is introduced.

# The allowed padding/margin/gap steps, as Tailwind multipliers
# (x4 = px): 0.5/1/1.5/2/3/4/6/8/12/16 == 2/4/6/8/12/16/24/32/48/64 px.
_SPACING_SCALE_STEPS: Final[frozenset[float]] = frozenset({0, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12, 16})
# Only padding / margin / gap follow the scale; width / height / inset are
# free layout dimensions and are intentionally NOT scanned.
_SPACING_PREFIXES: Final[tuple[str, ...]] = (
    "p",
    "px",
    "py",
    "pt",
    "pr",
    "pb",
    "pl",
    "ps",
    "pe",
    "m",
    "mx",
    "my",
    "mt",
    "mr",
    "mb",
    "ml",
    "ms",
    "me",
    "gap",
    "gap-x",
    "gap-y",
    "space-x",
    "space-y",
)


def _strip_svg_path_data(text: str) -> str:
    """Remove SVG ``d="..."`` path attributes so their command+coord runs
    (``h-1``, ``m-0.5``, ``v6`` ...) are not misread as spacing utilities."""
    text = re.sub(r'(?<![\w-])d="[^"]*"', "", text)
    return re.sub(r"(?<![\w-])d='[^']*'", "", text)


def _design_system_source_files() -> list[Path]:
    client_root = Path(_templates_module.__file__).resolve().parent
    files = [
        path
        for directory in (client_root / "templates", client_root / "static")
        for path in sorted(directory.rglob("*"))
        if path.suffix in (".jinja", ".js") and path.name != "app.min.css"
    ]
    files.append(client_root / "templates.py")
    return files


def test_spacing_utilities_stay_on_scale() -> None:
    """Padding / margin / gap utilities must use the constrained spacing scale
    -- Tailwind steps 0.5 / 1 / 1.5 / 2 / 3 / 4 / 6 / 8 / 12 / 16 (= 2 / 4 / 6 /
    8 / 12 / 16 / 24 / 32 / 48 / 64 px). A new off-scale value (e.g. ``py-2.5``,
    10px) fails here; snap it to the nearest step or, if it is a deliberate
    layout dimension, use width / height / inset instead (those are free and not
    scanned)."""
    alt = "|".join(sorted((re.escape(p) for p in _SPACING_PREFIXES), key=lambda s: len(s), reverse=True))
    token = re.compile(r"(?<![\w-])-?(" + alt + r")-([0-9]+(?:\.[0-9]+)?)(?![\w./\[])")
    offenders: list[str] = []
    for path in _design_system_source_files():
        text = _strip_svg_path_data(path.read_text())
        for match in token.finditer(text):
            if float(match.group(2)) not in _SPACING_SCALE_STEPS:
                offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == [], (
        "Off-scale padding/margin/gap utilities found. The constrained spacing "
        "scale is the Tailwind steps 0.5/1/1.5/2/3/4/6/8/12/16 "
        f"(= 2/4/6/8/12/16/24/32/48/64 px). Snap to the nearest step: {offenders}"
    )


def test_radius_utilities_stay_on_scale() -> None:
    """Corner radius is limited to ``rounded-sm`` / ``-md`` / ``-lg`` / ``-xl``
    (4/6/8/16 px) plus ``rounded-full`` / ``rounded-none``. The old
    ``rounded-2xl`` / ``-3xl`` / ``-xs`` steps and arbitrary ``rounded-[..]``
    values are disallowed -- the sole exception is the chrome content frame's
    structural ``rounded-[12px]`` (matches Electron's CONTENT_CORNER_RADIUS)."""
    disallowed = re.compile(r"\brounded-(?:2xl|3xl|4xl|xs)\b|\brounded-\[(?!12px\])[^\]]*\]")
    offenders: list[str] = []
    for path in _design_system_source_files():
        for match in disallowed.finditer(path.read_text()):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == [], (
        "Disallowed corner-radius utilities found. Use rounded-sm/-md/-lg/-xl "
        f"(4/6/8/16 px) or rounded-full/-none: {offenders}"
    )


def test_text_uses_type_roles_not_raw_size_or_medium() -> None:
    """Content text must use the type ramp roles (``type-heading-lg`` /
    ``type-heading`` / ``type-label`` / ``type-body`` / ``type-helper`` /
    ``type-section``), which bundle font-size + weight + line-height. Raw
    font-size utilities (``text-sm``, ``text-[13px]`` ...) and ``font-medium``
    (dropped from the ramp -- it's 400 / 600 only) are disallowed. Inline
    ``font-normal`` / ``font-semibold`` / ``font-bold`` for emphasis within a
    role are still allowed; SVG path data is skipped."""
    banned = re.compile(r"\btext-(?:xs|sm|base|lg|xl|2xl|3xl)\b|\btext-\[[0-9.]+px\]|\bfont-medium\b")
    offenders: list[str] = []
    for path in _design_system_source_files():
        text = _strip_svg_path_data(path.read_text())
        for match in banned.finditer(text):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == [], (
        "Raw font-size / font-medium found. Use a type-* role (it bundles "
        f"size + weight + line-height); the ramp weights are 400/600/bold: {offenders}"
    )


def test_elevation_uses_shadow_roles_not_raw_steps() -> None:
    """Box-shadow is limited to the two elevation roles -- ``shadow-raised``
    (interactive-card hover lift) and ``shadow-overlay`` (floating menus /
    modals / tooltips) -- plus ``shadow-none``. Tailwind's raw shadow steps
    (``shadow-sm`` ... ``shadow-2xl``, ``shadow-inner``) and arbitrary
    ``shadow-[..]`` are disallowed. (Inline ``box-shadow:`` in a style attribute
    -- e.g. the content-frame inset highlight -- is a raw CSS property, not a
    utility, and is not matched.)"""
    banned = re.compile(r"\bshadow-(?:2xs|xs|sm|md|lg|xl|2xl|inner)\b|\bshadow-\[[^\]]*\]")
    offenders: list[str] = []
    for path in _design_system_source_files():
        for match in banned.finditer(path.read_text()):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == [], (
        f"Raw box-shadow utilities found. Use shadow-raised / shadow-overlay (or shadow-none): {offenders}"
    )


def test_notice_renders_each_variant() -> None:
    # Each variant paints a per-mode surface token (--c-*-surface): a faint tint
    # in light, a higher-opacity tint in dark so the shape stays visible on black.
    variants_to_class = {
        "info": "--c-info-surface",
        "warn": "--c-warning-surface",
        "success": "--c-success-surface",
        "error": "--c-important-surface",
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
    assert "py-2" in html
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
    assert "type-label" in html
    assert "text-primary" in html


def test_form_label_inline_drops_block_and_mb() -> None:
    html = CATALOG.render("FormLabel", target="x", inline=True, _content="Provider")
    # Inline layout: no block / mb classes (the parent flex row handles
    # spacing), but the shared type role + color remain.
    assert "block" not in html
    assert "mb-1.5" not in html
    assert "type-label" in html


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


def test_page_narrow_container_default_padding_and_max_width() -> None:
    html = CATALOG.render("PageNarrowContainer", title="x", _content="<p>body</p>")
    # The narrow column itself is width/padding only: p-8 + max-w-[420px] +
    # w-full, no surface chrome (it is a plain width container, not a card).
    assert "p-8" in html
    assert "max-w-[420px]" in html
    assert "w-full" in html
    assert "<p>body</p>" in html
    # PageNarrowContainer now renders via the shared ChromeShell layout, so a
    # trusted local page reached through it (auth flow, create form) carries the
    # app titlebar; the column is flex-centered inside the local-page card
    # (``min-h-full`` fills the card so short content centers) rather than on the
    # body, which is viewport-locked now.
    assert 'id="minds-titlebar"' in html
    assert "min-h-full flex items-center justify-center" in html


def test_page_narrow_container_form_padding_uses_p6() -> None:
    html = CATALOG.render("PageNarrowContainer", title="x", padding="form", max_width="max-w-[520px]", _content="x")
    assert "p-6" in html
    assert "p-8" not in html
    assert "max-w-[520px]" in html


def test_icon16_renders_with_fill_shell_and_default_size() -> None:
    # ``home`` is one of the icons in the ICONS_16 catalog global.
    html = CATALOG.render("Icon16", name="home")
    # The 16x16 fill shell: the SVG defaults to fill="currentColor" so each
    # glyph takes the parent's text color (Figma's hardcoded black is dropped).
    assert 'viewBox="0 0 16 16"' in html
    assert 'fill="currentColor"' in html
    assert 'aria-hidden="true"' in html
    # The fill icons carry no stroke shell (that was the old lucide style).
    assert 'stroke-width="2"' not in html
    # Default size = md = w-4 h-4.
    assert "w-4 h-4" in html
    # Path data flows through unescaped as a bare fill outline (no per-path
    # fill -- it inherits currentColor from the shell, never Figma's black).
    assert '<path d="M9.40039 9.01301' in html
    assert "black" not in html


def test_icon16_size_axis() -> None:
    for size, css_class in (("sm", "w-3.5 h-3.5"), ("md", "w-4 h-4"), ("lg", "w-5 h-5")):
        html = CATALOG.render("Icon16", name="home", size=size)
        assert css_class in html


def test_icon16_renders_arrow_up_right() -> None:
    # The diagonal open-in-new arrow backs the "open in new window"
    # affordance on workspace rows (landing page).
    html = CATALOG.render("Icon16", name="arrow-up-right")
    assert 'viewBox="0 0 16 16"' in html
    assert '<path d="M12.9331 10.3336' in html


def test_icon16_renders_menu() -> None:
    # The ``menu`` glyph (three horizontal bars) is the titlebar button that
    # opens the floating workspace menu.
    html = CATALOG.render("Icon16", name="menu")
    assert 'viewBox="0 0 16 16"' in html
    assert '<path d="M13.3337 11.4004' in html


def test_icon16_play_is_the_lone_stroked_glyph() -> None:
    # Every other glyph is a filled outline, but ``play`` is a stroked
    # triangle, so its path overrides the shell's fill with its own
    # currentColor stroke (still no hardcoded black).
    html = CATALOG.render("Icon16", name="play")
    assert 'viewBox="0 0 16 16"' in html
    assert 'fill="none" stroke="currentColor" stroke-width="1.2"' in html
    assert "black" not in html


def test_icon16_badge_check_renders_as_an_outline_glyph() -> None:
    # ``badge-check`` (the unfilled badge on the local preset card) is a single
    # currentColor fill outline like the rest of the set -- no hardcoded black,
    # no evenodd knockout.
    html = CATALOG.render("Icon16", name="badge-check")
    assert 'viewBox="0 0 16 16"' in html
    assert '<path d="M14.0635 7.99966' in html
    assert "black" not in html
    assert "fill-rule" not in html


def test_icon16_badge_check_filled_is_a_solid_knockout_glyph() -> None:
    # ``badge-check-filled`` (the blue badge on the remote preset card) is the
    # lone solid glyph: a filled badge with the check knocked out via
    # ``fill-rule="evenodd"``, so the cut-out shows the surface behind it. It
    # still inherits currentColor from the shell (no hardcoded black).
    html = CATALOG.render("Icon16", name="badge-check-filled")
    assert 'viewBox="0 0 16 16"' in html
    assert 'fill-rule="evenodd"' in html
    assert 'clip-rule="evenodd"' in html
    assert "black" not in html


def test_icon16_settings_is_offset_into_the_16_grid() -> None:
    # ``settings`` is authored on a 15-unit grid, so it's nudged into the
    # 16-unit frame with a translate group.
    html = CATALOG.render("Icon16", name="settings")
    assert '<g transform="translate(0.5 0.5)">' in html


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
    # Inherits the shared INPUT_BASE accent focus ring (drawn outside the field).
    assert "focus:outline-accent" in html
    assert "focus:outline-2" in html
    # The chevron is overlaid via a themeable Icon16 (native arrow hidden).
    assert "appearance-none" in html
    # Default width sizes the wrapper; the inner <select> fills it (w-full).
    assert 'class="relative w-full"' in html


def test_select_honors_width_prop() -> None:
    html = CATALOG.render("Select", name="x", width="w-48", _content="")
    # The width prop sizes the wrapper; the inner <select> fills it (w-full).
    assert 'class="relative w-48"' in html


def test_link_regular_uses_accent_underline_recipe() -> None:
    html = CATALOG.render("Link", href="/x", _content="back").strip()
    assert "<a " in html
    assert 'href="/x"' in html
    assert "text-accent" in html
    assert "hover:underline" in html
    assert "font-medium" not in html


def test_link_medium_weight_adds_font_semibold() -> None:
    html = CATALOG.render("Link", href="/x", weight="medium", _content="Sign in")
    assert "font-semibold" in html


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
    assert "focus:outline-accent" in html


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
    assert "pt-4" in html


def test_dialog_close_button_renders_x_svg_and_onclick() -> None:
    html = CATALOG.render("DialogCloseButton", onclick="closePermissionDialog()")
    assert 'aria-label="Close"' in html
    assert 'onclick="closePermissionDialog()"' in html
    # Renders the shared Icon16 ``close`` glyph at 20px; its path fragment.
    assert "w-5 h-5" in html
    assert '<path d="M11.5762 3.57617' in html


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
    # Done / Failed / Info are solid status fills; neutral a muted fill; warn
    # the yellow caution surface (foreground stays the warning hue).
    variants_to_class = {
        "neutral": "bg-fill-subtle",
        "success": "bg-success text-white",
        "error": "bg-important text-white",
        "warn": "--c-warning-surface",
        "info": "bg-info text-white",
    }
    for variant, css_class in variants_to_class.items():
        html = CATALOG.render("StatusBadge", variant=variant, _content="x")
        assert css_class in html, f"variant={variant} missing {css_class}"


def test_status_badge_size_xs_uses_helper_role() -> None:
    html = CATALOG.render("StatusBadge", size="xs", _content="x")
    # xs inline tag reads as helper (12); sm slot badge reads as label (14).
    assert "type-helper" in html
    assert "type-label" not in html


def test_status_badge_title_renders_when_present() -> None:
    html = CATALOG.render("StatusBadge", title="why this is shown", _content="x")
    assert 'title="why this is shown"' in html


def test_status_badge_title_omitted_when_empty() -> None:
    html = CATALOG.render("StatusBadge", _content="x")
    assert "title=" not in html


def test_badge_dot_when_count_omitted() -> None:
    # No count -> the bare 8px important dot: no number, no pill width / type role.
    html = CATALOG.render("Badge")
    assert "w-2 h-2 rounded-full bg-important" in html
    assert "min-w-" not in html
    assert "type-badge" not in html


def test_badge_count_renders_number_in_pill() -> None:
    html = CATALOG.render("Badge", count=4)
    assert ">4<" in html
    # The count pill: min-width keeps a single digit circular; bold 10px role.
    assert "min-w-[16px]" in html
    assert "type-badge" in html
    assert "bg-important" in html


def test_badge_count_caps_at_99_plus() -> None:
    # Counts above 99 collapse to "99+" so the pill stays compact.
    html = CATALOG.render("Badge", count=150)
    assert ">99+<" in html
    assert "150" not in html


def test_badge_class_and_id_pass_through() -> None:
    # The titlebar requests badge relies on id + the chrome.js-toggled `hidden`
    # class flowing through onto the badge's root span. ``**{...}`` is required
    # because ``class`` is a reserved word; ty flags the dict[str, str] unpack as
    # possibly feeding render's typed ``caller`` kwarg, which it never does here.
    badge_attrs = {"id": "requests-badge", "class": "hidden absolute top-0.5 right-0.5"}
    html = CATALOG.render("Badge", **badge_attrs)  # ty: ignore[invalid-argument-type]
    assert 'id="requests-badge"' in html
    assert "hidden" in html
    assert "absolute" in html


def test_expected_duration_per_launch_mode() -> None:
    assert expected_create_attempt_duration_seconds(LaunchMode.DOCKER) == 30.0
    assert expected_create_attempt_duration_seconds(LaunchMode.IMBUE_CLOUD) == 30.0
    assert expected_create_attempt_duration_seconds(LaunchMode.LIMA) == 600.0
    assert expected_create_attempt_duration_seconds(LaunchMode.VULTR) == 300.0


def test_expected_duration_covers_every_launch_mode() -> None:
    # Every launch mode must resolve to a positive duration so the progress
    # bar never divides by zero; unmapped modes fall back to the default.
    for launch_mode in LaunchMode:
        assert expected_create_attempt_duration_seconds(launch_mode) > 0
    assert DEFAULT_EXPECTED_CREATE_ATTEMPT_DURATION_SECONDS == 60.0


def test_base_omits_sentry_bootstrap_when_frontend_reporting_is_off() -> None:
    # Rendered outside any Flask app context, so the catalog global resolves no MindsConfig and
    # defaults to reporting disabled: no page may pull in the Sentry browser bundle or its init.
    html = render_login_page()
    assert "sentry.browser.min.js" not in html
    assert "sentry_init.js" not in html
    assert "minds-sentry-config" not in html


def test_base_emits_sentry_bootstrap_when_frontend_reporting_is_on() -> None:
    # Rendered through a freshly built catalog whose Sentry global is overridden
    # to return a payload. A fresh catalog is used (rather than mutating the
    # shared CATALOG global) because reassigning a Jinja env global mid-process
    # interacts with Jinja's template cache; in production the global never
    # changes, so this is purely a test concern.
    payload = {
        "dsn": "https://key@o1.ingest.us.sentry.io/2",
        "environment": "staging",
        "release": "0.3.2",
        "git_sha": "abc1234",
    }
    catalog = _templates_module._build_catalog()
    # ty narrows the Jinja globals dict to a union of the seeded value types,
    # which excludes an arbitrary ``() -> dict`` test stub; the assignment is
    # fine at runtime (Jinja globals are untyped string-keyed values).
    catalog.jinja_env.globals["frontend_sentry_browser_payload"] = lambda: payload  # ty: ignore[invalid-assignment]
    html = catalog.render("pages.Login")
    # Bundle + init load before the page's own scripts; config is passed as JSON.
    assert '<script src="/_static/sentry.browser.min.js"></script>' in html
    assert '<script src="/_static/sentry_init.js"></script>' in html
    assert '<script type="application/json" id="minds-sentry-config">' in html
    assert '"environment": "staging"' in html
    assert '"dsn": "https://key@o1.ingest.us.sentry.io/2"' in html


def _plan_view_fixture(is_over_storage_quota: bool = False) -> dict[str, object]:
    return {
        "plan_name": "ally",
        "plan_display_name": "Ally",
        "available_plans": ["ally", "explorer"],
        "usage_rows": [
            {"label": "Remote machines", "used": "1", "limit": "10", "note": ""},
            {"label": "Backup storage", "used": "2.4 GB", "limit": "500.0 GB", "note": "n"},
        ],
        "is_over_storage_quota": is_over_storage_quota,
    }


def test_render_accounts_page_renders_async_plan_placeholder() -> None:
    # The page must never block on the connector: each account gets a loading
    # placeholder that accounts.js fills in from GET /accounts/<uid>/plan-view.
    acct = SimpleNamespace(user_id="u-1", email="a@b.com", workspace_ids=[])
    html = render_accounts_page(accounts=[acct], default_account_id="u-1")
    assert "data-plan-section" in html
    assert 'data-user-id="u-1"' in html
    assert "Loading plan and usage" in html
    assert '<script src="/_static/accounts.js" defer></script>' in html


def test_render_account_plan_section_renders_usage_and_plan_selector() -> None:
    html = render_account_plan_section(acct_user_id="u-1", plan_view=_plan_view_fixture())
    assert 'data-trim-running="0"' in html
    assert "Ally" in html
    assert "1 of 10" in html
    assert "2.4 GB of 500.0 GB" in html
    assert "/accounts/u-1/plan" in html
    assert "Switch plan" in html


def test_render_account_plan_section_shows_trim_action_only_when_over_quota_and_idle() -> None:
    over_html = render_account_plan_section(acct_user_id="u-1", plan_view=_plan_view_fixture(True))
    assert "/accounts/u-1/trim-backups" in over_html
    under_html = render_account_plan_section(acct_user_id="u-1", plan_view=_plan_view_fixture(False))
    assert "/accounts/u-1/trim-backups" not in under_html


def test_render_account_plan_section_marks_running_trim_for_polling() -> None:
    trim = SimpleNamespace(is_running=True, detail="Trimming backups (round 1)")
    html = render_account_plan_section(acct_user_id="u-1", plan_view=_plan_view_fixture(True), trim_status=trim)
    assert 'data-trim-running="1"' in html
    assert "Trimming backups (round 1)" in html
    # The trim form is hidden while a trim is already running.
    assert "/accounts/u-1/trim-backups" not in html


def test_render_account_plan_section_degrades_to_unavailable_without_plan_view() -> None:
    html = render_account_plan_section(acct_user_id="u-1")
    assert "Plan and usage are unavailable right now" in html
    assert 'data-trim-running="0"' in html


def test_render_create_form_preselects_the_retry_cloud_account_and_machine_size() -> None:
    # The retry pre-fill restores a bring-your-own-key create exactly: the
    # account's BYOK option starts selected (the cloud modes have no plain
    # options, so this is the only way the selection can be restored) and the
    # stored machine size is threaded into the instance-type populate JS.
    account = CloudAccountRecord(
        name="byok-gcp-retrytest",
        alias="retrytest",
        backend="gcp",
        region="us-west1-a",
        identifier="masked",
    )
    html = render_create_form(
        cloud_accounts=[account],
        byok_clouds_enabled=True,
        selected_cloud_account="byok-gcp-retrytest",
        selected_instance_type="e2-standard-4",
    )
    assert re.search(r'value="BYOK:byok-gcp-retrytest"[^>]*\sselected', html) is not None
    assert 'var instanceTypePreselect = "e2-standard-4";' in html


def test_render_create_form_leaves_byok_options_unselected_by_default() -> None:
    account = CloudAccountRecord(
        name="byok-aws-other",
        alias="other",
        backend="aws",
        region="us-east-1",
        identifier="masked",
    )
    html = render_create_form(cloud_accounts=[account], byok_clouds_enabled=True)
    assert re.search(r'value="BYOK:byok-aws-other"[^>]*\sselected', html) is None
    assert 'var instanceTypePreselect = "";' in html


def test_render_account_plan_modal_page_opens_instantly_with_async_placeholder() -> None:
    # The shell must not embed usage: it renders instantly with a spinner and
    # accounts.js fills the placeholder from GET /accounts/<uid>/plan-view.
    html = render_account_plan_modal_page(acct_user_id="u-1", account_email="a@b.com")
    assert "a@b.com" in html
    assert 'id="account-plan-modal-backdrop"' in html
    assert "data-plan-section" in html
    assert 'data-user-id="u-1"' in html
    assert "Loading plan and usage" in html
    assert '<script src="/_static/accounts.js" defer></script>' in html


def test_render_accounts_modal_page_cards_open_the_plan_modal() -> None:
    acct = SimpleNamespace(user_id="u-1", email="a@b.com", workspace_ids=[])
    html = render_accounts_modal_page(accounts=[acct], default_account_id="u-1")
    # Each card carries the drill-in hook and the modal wires the launcher.
    assert 'data-open-plan="u-1"' in html
    assert "openAccountPlan" in html


def test_render_accounts_modal_page_account_actions_carry_the_busy_affordances() -> None:
    """Both slow account actions ship the hooks their in-flight state needs.

    Sign-out runs for seconds (plugin signout, provider teardown, supervisor
    bounce), so the clicked button swaps its label and reveals a spinner. The
    script binds by these class names, so a card without them would leave the
    button looking untouched for the whole wait -- the bug this fixes.
    """
    # Not the default account, so both actions render on the card.
    acct = SimpleNamespace(user_id="u-1", email="a@b.com", workspace_ids=[])
    html = render_accounts_modal_page(accounts=[acct], default_account_id="u-other")

    assert html.count('class="account-action-spinner hidden"') == 2
    assert html.count('class="account-action-label"') == 2
    assert "Logging out" in html
    assert "Switching" in html
    # Failures surface in the notice instead of silently restoring the buttons.
    assert 'id="accounts-modal-error"' in html
    assert "Could not log out of this account" in html


def test_render_destroyed_workspaces_page_shell_is_async_without_rows() -> None:
    # The shell must paint instantly: it carries the retention copy and the
    # async fetch hook, but embeds no rows (those come from the rows fragment).
    html = render_destroyed_workspaces_page(retention_days=30, error="")
    assert "30 days" in html
    assert "data-destroyed-rows" in html
    assert "/workspaces/destroyed/rows" in html
    assert "Loading recently destroyed machines" in html


def test_render_destroyed_workspaces_page_shows_error_in_shell() -> None:
    html = render_destroyed_workspaces_page(retention_days=30, error="Something went wrong")
    assert "Something went wrong" in html


def _destroyed_row_fixture() -> dict[str, object]:
    return {
        "agent_id": "agent-abc",
        "display_name": "old-workspace",
        "account_label": "test@example.com",
        "destroyed_at_display": "2026-07-01",
        "days_left_display": "27 day(s) until deletion",
        "has_backup": True,
        "can_download": True,
        "is_locked": False,
        "can_delete": True,
        "delete_hint": "",
    }


def test_render_destroyed_workspaces_rows_fragment_arms_confirm_with_flex_not_inline_flex() -> None:
    # The armed confirm must use ``flex`` so ``.hidden`` wins the cascade; an
    # ``inline-flex`` here would leave both delete states visible at once.
    html = render_destroyed_workspaces_rows_fragment(rows=[_destroyed_row_fixture()])
    assert "old-workspace" in html
    assert ">Remove<" in html
    assert "hidden flex flex-col items-end gap-1" in html
    # Bare ``inline-flex`` is fine on the always-visible Buttons; the bug is
    # specifically pairing ``hidden`` with ``inline-flex`` on the armed span.
    assert "hidden inline-flex" not in html
    # The actions column must not wrap; the name column shrinks instead.
    assert "shrink-0" in html


def test_render_destroyed_workspaces_rows_fragment_empty_state() -> None:
    html = render_destroyed_workspaces_rows_fragment(rows=[])
    assert "No recently destroyed machines" in html


# -- Workspace options panel (Share machine / Machine settings) --


_OPTIONS_SERVERS: Final[tuple[str, ...]] = ("mailroom", "newsreader", "system_interface")


def _options_modal(
    tab: str = "share",
    servers: Sequence[str] = _OPTIONS_SERVERS,
    selected_target: str = "",
    has_account: bool = True,
    accounts: Sequence[object] = (),
    current_color: str = DEFAULT_WORKSPACE_COLOR,
    anchor_x: int | None = 214,
    anchor_y: int | None = 5,
    anchor_height: int | None = 28,
) -> str:
    """Render the options panel with the fixtures every test below shares."""
    return render_workspace_options_modal_page(
        agent_id=str(_AGENT_A),
        ws_name="aurora",
        current_account=None,
        accounts=accounts,
        servers=servers,
        tab=tab,
        selected_target=selected_target,
        account_email="owner@example.com",
        anchor_x=anchor_x,
        anchor_y=anchor_y,
        anchor_height=anchor_height,
        current_color=current_color,
        has_account=has_account,
    )


def test_workspace_options_modal_centers_and_drops_the_tabs_without_an_anchor() -> None:
    # Opened from the workspace list there is no titlebar icon-tab strip to hang
    # from, so the panel is an ordinary centered dialog -- and carries no tabs,
    # which drawn away from the buttons they stand in for would be anchored to
    # nothing. It still shows the tab it was opened on.
    html = _options_modal(tab="settings", anchor_x=None, anchor_y=None, anchor_height=None)
    assert 'role="tablist"' not in html
    assert "items-center justify-center" in html
    assert 'data-settings-group="general"' in html
    # Nothing may be positioned against an anchor that was never measured.
    assert "None" not in html


def test_workspace_options_modal_docks_the_card_under_the_measured_tab_strip() -> None:
    # The panel hangs from the titlebar's icon-tab strip: the tab strip is drawn
    # at the measured x and the card's top edge meets the strip's bottom, so the
    # selected tab reads as joined to the panel. The card's width leaves the same
    # margin on the right that the anchor leaves on the left.
    html = _options_modal(anchor_x=214, anchor_y=5, anchor_height=28)
    assert "left: 214px" in html
    assert "top: -28px" in html
    # anchor_y + anchor_height == the strip's bottom == the card region's top.
    assert "top: 33px" in html
    assert "max(880px, calc(100% - 2 * (214px - 20px)))" in html


def test_workspace_options_modal_renders_both_tabs_with_only_one_selected() -> None:
    html = _options_modal(tab="share")
    assert 'id="ws-options-tab-share"' in html
    assert 'id="ws-options-tab-settings"' in html
    assert html.count('aria-selected="true"') == 1
    # The selected tab is filled with the card's surface and square-bottomed;
    # the unselected one self-themes against the accent-tinted titlebar.
    share_tab = html[html.index('id="ws-options-tab-share"') :][:600]
    assert "rounded-b-none" in share_tab
    assert "titlebar-surface" not in share_tab
    settings_tab = html[html.index('id="ws-options-tab-settings"') :][:600]
    assert "titlebar-surface" in settings_tab


def test_workspace_options_modal_ships_both_panes_so_switching_never_reloads() -> None:
    html = _options_modal(tab="share")
    assert 'data-wsopt-panel="share"' in html
    assert 'data-wsopt-panel="settings"' in html
    # Only the inactive one starts hidden.
    assert is_workspace_options_pane_hidden(html, "settings")
    assert not is_workspace_options_pane_hidden(html, "share")


# The classes WorkspaceShareSection / WorkspaceSettingsSections put on their
# right-hand panel when ``panel_scroll`` is on: it takes the leftover height and
# scrolls there, with its scrollbar pulled out to the card's edge and enough
# inner room that a focus ring against an edge is not clipped away.
_PANEL_SCROLL_CLASSES = "overflow-y-auto min-h-0 pt-1.5 pb-1.5 pl-1.5 pr-6 -mt-1.5 -mb-1.5 -ml-1.5 -mr-6"


def test_workspace_options_modal_scrolls_only_the_right_pane_so_the_title_and_tabs_stay_put() -> None:
    """The card is a fixed height, so the pane inside it -- not the card body -- scrolls.

    Scrolling the whole body took the title and the tab list with it. Each pane
    is a column whose right-hand panel takes the leftover height and scrolls
    there, which is what ``panel_scroll`` on the two section components buys.
    """
    html = _options_modal(tab="share")
    for pane in ("share", "settings"):
        body = workspace_options_pane_html(html, pane)
        assert _PANEL_SCROLL_CLASSES in body, f"{pane} pane's right panel is not the scroller"
        # Everything before the nav is the pinned part -- the title, and the
        # pane element itself. A scroller there would carry the title away
        # with the content, which is the bug this replaced.
        above_nav = body[: body.index("<nav")]
        assert "overflow-y-auto" not in above_nav, f"{pane} pane scrolls as a whole"
        assert "shrink-0" in above_nav, f"{pane} pane's title is not pinned"
        # The nav scrolls on its own too, so a machine with twenty entries does
        # not run them off the bottom of the card. overflow-y-auto means it only
        # does so when they actually overflow -- there is no count to maintain.
        nav = body[body.index("<nav") :][: body[body.index("<nav") :].index(">") + 1]
        assert "overflow-y-auto" in nav, f"{pane} pane's nav cannot scroll"


def test_workspace_options_page_does_not_clip_its_panes_into_scrollers() -> None:
    """The browser twin is an ordinary scrolling page, so nothing inside it clips.

    ``panel_scroll`` exists for exactly this split: the same two sections draw a
    scrolling right-hand panel inside the fixed-height panel card, and plain
    ungated content on the page that simply grows.
    """
    html = render_workspace_options_page(
        agent_id=str(_AGENT_A),
        ws_name="aurora",
        current_account=None,
        accounts=(),
        servers=_OPTIONS_SERVERS,
        tab="share",
        account_email="owner@example.com",
        has_account=True,
    )
    # The page has its own scrollers that predate this (the chrome's page
    # scroll, a log box), so look for the gated pair specifically rather than
    # for overflow anywhere.
    assert _PANEL_SCROLL_CLASSES not in html
    # -mr-4 only ever pairs with that scroller; alone it would pull the panel
    # out past the page container's padding.
    assert "-mr-6" not in html


def test_share_pane_explains_every_wait_in_a_sentence_ending_in_an_ellipsis() -> None:
    """Each slow action turns its button into a spinner and says what it is doing.

    A bare spinner does not distinguish "creating a Cloudflare tunnel" (slow,
    normal) from a hang, so every wait carries a sentence. The trailing "..."
    is what marks it as in progress rather than finished.
    """
    html = _options_modal(tab="share")
    # The waits whose text is on screen from the start live in the markup.
    for sentence in ("Checking who this is shared with...", "Creating the link and granting access..."):
        assert sentence in html, f"the share pane never says {sentence!r}"

    # The status line is relabelled per action, so those sentences live in the
    # script that swaps them in.
    pane_script = (_DESKTOP_CLIENT_DIR / "static" / "workspace_options.js").read_text()
    for sentence in ("Stopping sharing and revoking the link...", "Updating who can open this link..."):
        assert sentence in pane_script, f"the share pane never says {sentence!r}"

    # The enable sentence sits beside its button, on the button's own row.
    enable_row = html[html.index('id="ws-share-enable-row"') :]
    enable_row = enable_row[: enable_row.index("</div>")]
    assert 'id="ws-share-enable-status"' in enable_row
    assert "Creating the link and granting access..." in enable_row


def test_unlinking_an_account_is_confirmed_first() -> None:
    """Unlink asks before acting, like destroy does.

    Unlinking tears down every tunnel for the machine and cannot be undone by
    linking again -- sharing has to be set up from scratch -- so the button
    opens a confirmation rather than firing the request.
    """
    account = SimpleNamespace(user_id="u", email="a@b")
    html = CATALOG.render(
        "WorkspaceSettingsSections",
        agent_id=str(_AGENT_A),
        ws_name="aurora",
        current_account=account,
        accounts=[account],
        palette=WORKSPACE_PALETTE,
    )
    assert 'id="unlink-dialog"' in html
    assert 'id="unlink-cancel-btn"' in html
    assert 'id="unlink-confirm-btn"' in html
    # The dialog names the consequence, not just the action.
    assert "removes all sharing" in html


def test_workspace_options_modal_passes_the_accent_to_the_tab_strip() -> None:
    # An unselected tab sits on the workspace-tinted titlebar, so it needs the
    # same --titlebar-bg self-theming the real titlebar buttons use.
    accent = "#492222"
    html = _options_modal(current_color=accent)
    assert f"--titlebar-bg: {accent}" in html


def test_workspace_options_share_pane_lists_apps_then_the_whole_machine() -> None:
    # The whole-machine target is the workspace's own web UI service; it is
    # listed separately (below a divider), never as one of the apps.
    html = _options_modal(tab="share")
    assert 'data-share-target="mailroom"' in html
    assert 'data-share-target="newsreader"' in html
    assert 'data-share-target="system_interface"' in html
    assert "Whole machine" in html
    apps_region = html[html.index('data-share-target="mailroom"') : html.index('data-share-target="system_interface"')]
    assert "system_interface" not in apps_region


def test_workspace_options_share_pane_offers_whole_machine_before_any_service_registers() -> None:
    # A workspace that has not published system_interface yet must still be
    # shareable as a whole -- that is the option most users want first.
    html = _options_modal(tab="share", servers=())
    assert 'data-share-target="system_interface"' in html
    assert "Whole machine" in html


def test_workspace_options_share_pane_defaults_to_the_whole_machine_target() -> None:
    html = _options_modal(tab="share")
    config = html[html.index('id="ws-share-config"') :]
    assert '"selectedTarget": "system_interface"' in config
    assert '"accountEmail": "owner@example.com"' in config


def test_workspace_options_share_pane_honors_an_explicit_target() -> None:
    html = _options_modal(tab="share", selected_target="newsreader")
    assert '"selectedTarget": "newsreader"' in html


def test_workspace_options_share_pane_asks_for_an_account_before_offering_targets() -> None:
    # Sharing runs through the account's Cloudflare tunnel, so an unassociated
    # workspace gets the Associate prompt rather than an editor that cannot work.
    html = _options_modal(tab="share", has_account=False)
    assert "data-share-target" not in html
    assert 'id="ws-share-config"' not in html


def test_workspace_options_gives_each_associate_prompt_its_own_element_ids() -> None:
    # An unassociated workspace shows an Associate prompt in BOTH panes (the
    # share pane's, and Machine settings' account group), and both are rendered
    # up front. Associate binds its form by id in an inline script, so colliding
    # ids would leave both scripts on the first form and the second form with no
    # submit handler -- its Associate button would then do a native GET
    # submission and reload the panel without its tab / anchor params.
    acct = SimpleNamespace(user_id="u-1", email="a@b.com")
    html = _options_modal(tab="share", has_account=False, accounts=(acct,))
    assert html.count('id="associate-form"') == 1
    assert html.count('id="associate-form-share"') == 1
    assert html.count('id="associate-error"') == 1
    assert html.count('id="associate-error-share"') == 1


def test_workspace_options_settings_pane_groups_sections_behind_a_nav() -> None:
    html = _options_modal(tab="settings")
    for group in ("general", "account", "backup"):
        assert f'data-settings-group="{group}"' in html
        assert f'data-settings-pane="{group}"' in html
    # Sharing is the other tab's job -- the settings pane must not list services.
    assert "data-sharing-service" not in html


def test_workspace_options_pane_titles_name_the_machine() -> None:
    # A title alone does not say WHICH machine, and the panel can be opened from
    # the workspace list where the titlebar crumb does not name it either. The
    # name is capped so a long one truncates instead of wrapping the title or
    # crowding the close button.
    for tab, label in (("settings", "Machine settings"), ("share", "Share machine")):
        html = _options_modal(tab=tab)
        title = html[html.index(f">{label}:</span>") :][:300]
        assert ">aurora</span>" in title
        # The name is part of the title -- it inherits the heading's role and
        # color, and carries only the cap that stops a long one wrapping it.
        assert '<span class="truncate max-w-[280px]">aurora</span>' in title


def test_workspace_options_settings_pane_shows_the_machine_id_in_general() -> None:
    # The stable identifier lives in the General group as read-only info -- the
    # name and its host slug both change on rename, so neither is what you quote
    # in a report. It must not be in an input.
    html = _options_modal(tab="settings")
    general_pane = html[html.index('data-settings-pane="general"') :]
    general_pane = general_pane[: general_pane.index('data-settings-pane="account"')]
    assert f">{_AGENT_A}</p>" in general_pane
    assert f'value="{_AGENT_A}"' not in general_pane


def test_workspace_options_settings_groups_use_the_designed_icons() -> None:
    # General / Account / Backup carry the prototype's info / user / cloud
    # glyphs; a settings gear on "General" repeated the tab's own icon.
    html = _options_modal(tab="settings")
    nav = html[html.index('data-settings-group="general"') :]
    nav = nav[: nav.index('data-settings-pane="general"')]
    for icon_name in ("info", "user", "cloud"):
        assert CATALOG.render("Icon16", name=icon_name, extra="shrink-0") in nav


def test_workspace_share_targets_exclude_the_workspaces_own_interfaces() -> None:
    # Chat / terminal / browser / web are what the workspace is made of, not
    # apps to hand out one at a time; the whole machine is the deliberate way
    # to grant everything. Real apps beside them still appear.
    html = _options_modal(
        tab="share",
        servers=("terminal", "browser", "chat", "web", "newsreader", "system_interface"),
    )
    assert 'data-share-target="newsreader"' in html
    assert 'data-share-target="system_interface"' in html
    for excluded in ("terminal", "browser", "chat", "web"):
        assert f'data-share-target="{excluded}"' not in html


def test_workspace_options_share_pane_names_the_machine_in_its_blurb() -> None:
    # The name lands mid-sentence here ("access the tools or all of <name>"),
    # which is where a fallback to the agent id read worst.
    html = _options_modal(tab="share")
    assert 'all of <span class="font-semibold">aurora</span>' in html


def test_workspace_options_settings_pane_keeps_the_ids_its_scripts_bind() -> None:
    html = _options_modal(tab="settings")
    for element_id in (
        "workspace-settings",
        "workspace-name-input",
        "rename-save-btn",
        "color-swatches",
        "color-hex-input",
        "destroy-btn",
        "destroy-dialog",
        "backup-history",
        "restore-dialog",
    ):
        assert f'id="{element_id}"' in html


def test_workspace_options_page_is_the_browser_twin_without_the_overlay_chrome() -> None:
    html = render_workspace_options_page(
        agent_id=str(_AGENT_A),
        ws_name="aurora",
        current_account=None,
        accounts=(),
        servers=_OPTIONS_SERVERS,
        tab="share",
        account_email="owner@example.com",
        has_account=True,
    )
    assert 'data-wsopt-panel="share"' in html
    assert 'data-share-target="system_interface"' in html
    # No backdrop and no tab strip: the titlebar's own icon-tabs navigate here.
    assert 'id="ws-options-backdrop"' not in html
    assert 'role="tablist"' not in html
    assert "{{" not in html


def test_render_workspace_settings_delegates_to_the_shared_settings_sections() -> None:
    # The standalone page and the panel render the same body, so the two
    # surfaces cannot drift.
    html = render_workspace_settings(
        agent_id=str(_AGENT_A),
        ws_name="ws",
        current_account=None,
        accounts=(),
    )
    assert 'data-settings-group="general"' in html
    assert 'data-settings-pane="backup"' in html
    # The per-service sharing list moved to the Share machine tab.
    assert "data-sharing-service" not in html


def test_associate_offers_add_account_only_when_asked() -> None:
    # Opt-in: the surfaces that had a bare account picker keep one, so this
    # cannot change what workspace settings shows.
    account = SimpleNamespace(user_id="u-1", email="a@b.com")
    add_option = f'<option value="{ADD_ACCOUNT_OPTION_VALUE}">'
    plain = CATALOG.render("Associate", agent_id=str(_AGENT_A), accounts=[account])
    assert add_option not in plain
    with_add = CATALOG.render("Associate", agent_id=str(_AGENT_A), accounts=[account], can_add_account=True)
    assert add_option in with_add


def test_associate_ships_no_inline_script() -> None:
    # Its behavior lives in /_static/associate.js. An inline script here would
    # be adopted but never re-executed by the shell's page-swap engine, leaving
    # the form to do a native GET -- which looks like the button doing nothing
    # while dropping the page's query params.
    html = CATALOG.render("Associate", agent_id=str(_AGENT_A), accounts=[SimpleNamespace(user_id="u", email="a@b")])
    assert "<script" not in html
    # The hooks that script binds on.
    assert "data-associate" in html
    assert "data-associate-form" in html
    assert "data-associate-error" in html


def test_every_surface_rendering_associate_loads_its_script() -> None:
    # The component cannot load its own script: it would land in the swapped
    # page body rather than the local-page-scripts block, so each page must.
    account = SimpleNamespace(user_id="u-1", email="a@b.com")
    surfaces = [
        render_workspace_options_modal_page(
            agent_id=str(_AGENT_A),
            ws_name="ws",
            current_account=None,
            accounts=(account,),
            servers=(),
            has_account=False,
        ),
        render_workspace_options_page(
            agent_id=str(_AGENT_A),
            ws_name="ws",
            current_account=None,
            accounts=(account,),
            servers=(),
            has_account=False,
        ),
        render_workspace_settings(
            agent_id=str(_AGENT_A),
            ws_name="ws",
            current_account=None,
            accounts=(account,),
        ),
        render_sharing_editor(
            agent_id=str(_AGENT_A),
            service_name="frontend",
            title="Share",
            has_account=False,
            accounts=(account,),
        ),
    ]
    for html in surfaces:
        assert "data-associate-form" in html
        assert "/_static/associate.js" in html


def test_render_landing_page_caps_the_workspace_name() -> None:
    # A flex child defaults to min-width:auto, so a long name refused to shrink
    # and shoved the badges and the settings gear past the card's right edge.
    long_name = "a-really-extremely-long-workspace-name-that-would-otherwise-shove-the-buttons"
    html = render_landing_page(accessible_agent_ids=(_AGENT_A,), agent_names={str(_AGENT_A): long_name})
    # Anchor on the rendered span -- the name also appears earlier in the row's
    # data-mind-name attribute.
    span_end = html.index(f">{long_name}</span>")
    name_span = html[span_end - 200 : span_end]
    assert "min-w-0" in name_span
    assert "truncate" in name_span
