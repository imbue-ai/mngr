import re
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import pytest

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client import templates as _templates_module
from imbue.minds.desktop_client.templates import CATALOG
from imbue.minds.desktop_client.templates import DEFAULT_EXPECTED_CREATION_DURATION_SECONDS
from imbue.minds.desktop_client.templates import expected_creation_duration_seconds
from imbue.minds.desktop_client.templates import make_unique_host_name
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
from imbue.minds.desktop_client.templates import resolve_create_host_name
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
    # The open-in-new arrow glyph (Icon16 ``arrow-up-right``, Figma node 857-5137).
    assert '<path d="M12.9331 10.3336' in html
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
    # The repository git URL still has a hardcoded fallback (in the advanced
    # view); the compute provider select is present.
    assert "forever-claude-template" in html
    assert "launch_mode" in html


def test_render_create_form_has_optional_name_field() -> None:
    # The advanced view exposes an explicit "Name" (host_name) field so a user
    # can name the mind; left empty, the server auto-names it (mind-N).
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


def test_render_create_form_opens_signin_modal_via_overlay_relay() -> None:
    # Choosing Imbue Cloud while signed out opens the sign-in modal in the
    # desktop client's shared overlay layer (so it covers the title bar), not an
    # in-page dialog. The create page therefore no longer embeds the auth form
    # or loads auth.js itself; it asks the Electron main process to open the
    # /auth/signin-modal page via an allowlisted postMessage relay (falling back
    # to navigating there directly in the browser).
    html = render_create_form(accounts=[])
    assert "minds:open-signin-modal" in html
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
    for mode in LaunchMode:
        assert mode.value.lower() in html


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


def test_render_create_form_contains_ai_provider_options() -> None:
    html = render_create_form()
    for provider in AIProvider:
        assert f'value="{provider.value}"' in html


def test_render_create_form_defaults_ai_provider_to_imbue_cloud() -> None:
    # The remote preset is the default, so the AI provider starts on IMBUE_CLOUD
    # rather than the local SUBSCRIPTION default.
    html = render_create_form()
    assert 'value="SUBSCRIPTION" selected' not in html


def test_render_create_form_local_preset_selects_lima_and_subscription() -> None:
    # Selecting the local preset (e.g. a re-render of a LIMA submission) keeps
    # the compute / AI providers on the local LIMA / SUBSCRIPTION defaults.
    html = render_create_form(selected_preset="local")
    assert 'value="LIMA" selected' in html
    assert 'value="SUBSCRIPTION" selected' in html
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


def test_render_create_form_honors_workspace_env_vars_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the explicit opt-in, the MINDS_WORKSPACE_* env vars pre-fill the form.

    Used by ``just minds-start`` (and the e2e runner) to point the form at the
    operator's local FCT worktree + current branch so the dev-iteration loop is
    one click.
    """
    monkeypatch.setenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", "1")
    monkeypatch.setenv("MINDS_WORKSPACE_GIT_URL", "/local/fct/path")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "/local/fct/path" in html
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
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/some-feature")
    html = render_create_form()
    assert "/local/fct/path" not in html
    assert "mngr/some-feature" not in html
    # And the hardcoded git-URL fallback DOES appear (form is still usable).
    assert "forever-claude-template" in html


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


def test_resolve_create_host_name_uses_submitted_value() -> None:
    assert str(resolve_create_host_name("my-workspace")) == "my-workspace"


def test_resolve_create_host_name_generates_mind_name_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # No submitted name, no operator override, and no existing workspaces ->
    # the first ``mind-N`` name.
    monkeypatch.delenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", raising=False)
    monkeypatch.delenv("MINDS_WORKSPACE_NAME", raising=False)
    assert str(resolve_create_host_name("")) == "mind-1"


def test_resolve_create_host_name_picks_next_free_mind_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fallback skips names already in use across providers.
    monkeypatch.delenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", raising=False)
    monkeypatch.delenv("MINDS_WORKSPACE_NAME", raising=False)
    assert str(resolve_create_host_name("", {"mind-1", "mind-2"})) == "mind-3"


def test_resolve_create_host_name_honors_operator_override_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", "1")
    monkeypatch.setenv("MINDS_WORKSPACE_NAME", "mindtest")
    assert str(resolve_create_host_name("")) == "mindtest"


def test_resolve_create_host_name_operator_override_is_not_uniquified(monkeypatch: pytest.MonkeyPatch) -> None:
    # The operator override is used verbatim, even when it collides with an
    # existing workspace -- a duplicate name errors at create time (like a typed
    # name) rather than being silently renamed to ``mindtest-2``.
    monkeypatch.setenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", "1")
    monkeypatch.setenv("MINDS_WORKSPACE_NAME", "mindtest")
    assert str(resolve_create_host_name("", {"mindtest"})) == "mindtest"
    assert str(resolve_create_host_name("", {"mindtest", "mindtest-2"})) == "mindtest"


def test_resolve_create_host_name_ignores_operator_override_without_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without the opt-in, a stray MINDS_WORKSPACE_NAME is ignored and a
    # ``mind-N`` name is generated instead.
    monkeypatch.delenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", raising=False)
    monkeypatch.setenv("MINDS_WORKSPACE_NAME", "mindtest")
    assert str(resolve_create_host_name("")) == "mind-1"


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
    assert "sidebar-toggle" in html
    assert "home-btn" in html
    assert "back-btn" in html
    assert "content-frame" in html


def test_render_chrome_page_titlebar_centers_title_with_1_2_1_sections() -> None:
    # The titlebar is three flex sections sized 1 / 2 / 1 (left controls |
    # title | right controls) so the workspace title sits in the window's exact
    # horizontal center regardless of how wide each side's controls are. The
    # title's section grows at flex-[2] and centers its content; it is flanked
    # by exactly two flex-1 sections (left + right). The center must NOT be a
    # lone flex-1 -- that centered the title within the *leftover* space, so it
    # drifted off-center whenever the two sides differed in width.
    html = render_chrome_page()
    titlebar = html[html.index('id="minds-titlebar"') : html.index('id="sidebar-backdrop"')]
    assert "flex-[2] flex items-center justify-center" in titlebar
    assert "flex-[2]" in titlebar[: titlebar.index('id="page-title"')]
    assert titlebar.count("flex-1") == 2


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
    # The spacer sits at the very start of the left section, ahead of the menu
    # button (#sidebar-toggle), only on macOS.
    left_section_mac = html_mac[: html_mac.index('id="sidebar-toggle"')]
    assert 'class="w-[72px] shrink-0" aria-hidden="true"' in left_section_mac
    assert "w-[72px]" not in html_other


def test_render_chrome_page_requests_badge_is_inline_count() -> None:
    # The requests badge is the Badge count pill sat inline beside the messages
    # icon (gap-[3px] row), not a dot overlapping the icon's corner: it carries
    # the type-badge pill role and no absolute positioning (chrome.js fills the
    # count text + toggles the native `hidden` attribute).
    html = render_chrome_page()
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
    # "New workspace" CTA, the "Settings" entry, and the "Manage account(s)" /
    # "Log in" entry; the label is updated dynamically by sidebar.js from
    # /auth/api/status.
    assert 'id="sidebar-new-workspace"' in html
    assert 'id="sidebar-settings"' in html
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
    for tier in (
        "'host_offline'",
        "'interface_unresponsive'",
        "'host_unresponsive'",
        "'backend_unreachable'",
    ):
        assert tier in html, f"recovery page JS missing branch for {tier}"
    # The shared landing places for each branch.
    assert "renderUnresponsive" in html
    assert "renderBackendUnreachable" in html


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
    # The backend_unreachable branch arms the healthy-poll (auto-return when the
    # backend recovers) and returns before any restart dispatch.
    apply_start = html.find("function applyHealth(")
    apply_block = html[apply_start : html.find("function ", apply_start + 1)]
    assert apply_block.find("'backend_unreachable'") < apply_block.find("postRestart")
    assert "scheduleHealthyPoll()" in apply_block


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
    # Width/padding only: p-8 + max-w-[420px] + w-full, no surface chrome.
    assert "p-8" in html
    assert "max-w-[420px]" in html
    assert "w-full" in html
    assert "<p>body</p>" in html
    # No border/rounding/shadow -- this is a plain width container, not a card.
    assert "rounded-lg" not in html
    assert "shadow-raised" not in html
    assert "border border-default" not in html
    # The body is flex-centered around the column.
    assert "flex items-center justify-center min-h-screen" in html


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
    # Renders the shared Icon16 ``close`` glyph (16px); its path fragment.
    assert "w-4 h-4" in html
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
    assert expected_creation_duration_seconds(LaunchMode.DOCKER) == 30.0
    assert expected_creation_duration_seconds(LaunchMode.IMBUE_CLOUD) == 30.0
    assert expected_creation_duration_seconds(LaunchMode.LIMA) == 600.0
    assert expected_creation_duration_seconds(LaunchMode.VULTR) == 300.0


def test_expected_duration_covers_every_launch_mode() -> None:
    # Every launch mode must resolve to a positive duration so the progress
    # bar never divides by zero; unmapped modes fall back to the default.
    for launch_mode in LaunchMode:
        assert expected_creation_duration_seconds(launch_mode) > 0
    assert DEFAULT_EXPECTED_CREATION_DURATION_SECONDS == 60.0


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
