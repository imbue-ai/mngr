import pytest

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client.templates import render_auth_error_page
from imbue.minds.desktop_client.templates import render_chrome_page
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_recovery_page
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId

_AGENT_A: AgentId = AgentId("agent-00000000000000000000000000000001")
_AGENT_B: AgentId = AgentId("agent-00000000000000000000000000000002")


def test_render_landing_page_with_agents_lists_them_as_links() -> None:
    ids = (_AGENT_A, _AGENT_B)
    html = render_landing_page(accessible_agent_ids=ids)
    assert f"/goto/{_AGENT_A}/" in html
    assert f"/goto/{_AGENT_B}/" in html
    assert str(_AGENT_A) in html
    assert str(_AGENT_B) in html


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
