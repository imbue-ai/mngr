# Behavioral specification: sign-in, session, and landing

Component: the minds desktop client -- the bare-origin web UI served by `minds run`
(`apps/minds/imbue/minds/desktop_client/`) plus the workspace-origin bridge served
by the forward server (`libs/mngr_forward/`).

This file is a proof of concept for spec-driven behavioral testing: a behavioral
specification kept as a distinct artifact from the tests that validate it, with
explicit trace links between the two (see the traceability appendix).

## How to read this file

- **Scenarios** (Gherkin) describe flow paths: the steps a user or client takes
  and what they observe. Each scenario carries a stable tag (`@signin-fresh-code`)
  used as its identity in trace links.
- **Invariants** (`INV-*`) are properties that must hold across *all* scenarios
  and all states, not just the flows spelled out here. A single invariant may be
  verified by many tests scattered across many surfaces.
- The spec describes externally observable behavior only. How a test drives the
  system (test clients, waits, selectors, fixtures) is deliberately absent.
- HTTP details (paths, redirect targets, status codes) appear only where they are
  part of the observable contract of this HTTP surface.
- The **traceability appendix** maps scenario tags and invariants to the tests
  that currently verify them. `(partial)` means the test covers some but not all
  of the assertions; `(gap)` means no covering test was found.

## Glossary

- **desktop client**: the local server started by `minds run`; the gateway
  through which the user reaches all their workspaces.
- **data directory**: the desktop client's local state directory. One
  installation = one data directory.
- **one-time code**: a secret minted at server start. The server prints a
  **login URL** (`http://localhost:<port>/login?one_time_code=<code>`) to its
  terminal; this is the only credential a user ever types or clicks.
- **session**: the signed-in state of a browser, established by a successful
  sign-in and carried by a signed cookie. The session is global: it is the one
  credential gating every page and every workspace.
- **workspace**: an agent environment listed on the landing page. Each workspace
  is served on its own origin, `<agent-id>.localhost:<forward-port>`.
- **discovery**: the background process that finds the user's workspaces after
  startup. "Initial discovery" is its first complete pass.
- **consent screen**: the one-time "Help improve Minds" error-reporting question,
  asked once per machine right after sign-in.
- **goto bridge**: the forward server's `/goto/<agent-id>/` route, which converts
  a valid bare-origin session into a workspace-origin session without user
  interaction.

## Out of scope for this spec

- Imbue-cloud account sign-in (email/password/OAuth, the `/auth/*` pages) --
  a separate account system layered on top of the local session. Only its
  funnel point `/post-login` is specified here, because it decides landing.
- The workspace-creation flow beyond "the create form is shown".
- The contents of landing-page workspace rows (liveness, colors, destroy
  status, remote-device tiles, locked-account banners).
- The Electron shell's own startup routing (welcome/restore/create window
  decision) -- a candidate for a sibling spec.

## Scenarios

```gherkin
Feature: Sign-in with a one-time login code
  When the desktop client starts, it mints a fresh one-time code and prints
  the login URL to its terminal. Opening that URL in a browser is the only
  way to establish a session in a browser that has none.

  Background:
    Given a running desktop client
    And its terminal printed a login URL with a fresh one-time code

  @signin-fresh-code
  Scenario: Opening a fresh login URL signs the user in
    Given the user is not signed in
    When the user opens the login URL in a browser
    Then the browser lands on the home page "/"
    And the user is signed in
    And the one-time code is now spent

  @signin-used-code
  Scenario: A spent code cannot sign anyone in again
    Given the login URL has already been used to sign in
    When anyone presents the same code for authentication again
    Then authentication is refused, explaining the code is invalid or already used
    And no session is established

  @signin-unknown-code
  Scenario: A code the client never issued is refused
    Given the user is not signed in
    When they present a made-up code for authentication
    Then authentication is refused, explaining the code is invalid or already used
    And no session is established

  @signin-prefetch
  Scenario: Fetching the login URL without executing scripts does not spend the code
    Given the user is not signed in
    When something fetches the login URL without executing its scripts
      (a link preloader, a chat-app unfurler, a browser prerenderer)
    Then the code remains unspent
    And the user can still sign in later by opening the same URL in a real browser

  @signin-already-signed-in
  Scenario: Opening a login URL while already signed in does not spend the code
    Given the user is already signed in
    When they open a login URL carrying a fresh code
    Then they are redirected to the home page "/"
    And the code remains unspent

  @signin-missing-code
  Scenario Outline: Sign-in requests without a code are malformed input, not server errors
    When a request is made to "<path>" with no one-time code parameter
    Then it is rejected as malformed input (HTTP 422)

    Examples:
      | path          |
      | /login        |
      | /authenticate |
```

```gherkin
Feature: Session lifetime and integrity
  A successful sign-in establishes a session carried by a signed cookie.

  @session-survives-restart
  Scenario: Sessions survive a desktop-client restart
    Given a signed-in user
    When the desktop client is stopped and started again
    And the user reloads the home page
    Then they are still signed in
    And they do not need a new login code

  @session-tampered
  Scenario: An altered session token is treated as signed out
    Given a signed-in user
    When their session token is modified in any way
    And they request a signed-in page
    Then they are treated as signed out

  @session-foreign
  Scenario: A session minted by a different installation is not accepted
    Given a session token created by a desktop client with a different data directory
    When it is presented to this desktop client
    Then the bearer is treated as signed out

  @session-expiry
  Scenario: Sessions expire after 30 days
    Given a session token issued more than 30 days ago
    When it is presented
    Then the bearer is treated as signed out
```

```gherkin
Feature: Landing page routing
  "/" is the home page. What it shows depends on sign-in state, the one-time
  consent gate, and which workspaces are known.

  @landing-signed-out
  Scenario: Signed-out visitors see the sign-in prompt
    Given the user is not signed in
    When they visit "/"
    Then they see a sign-in prompt directing them to the login URL printed in the terminal
    And the page reveals nothing about existing workspaces

  @landing-consent-gate
  Scenario: The consent question is asked once, right after sign-in
    Given a signed-in user who has never answered the error-reporting consent question
    When they visit "/"
    Then they see the "Help improve Minds" consent screen instead of the landing content
    When they answer the consent question
    Then no later visit to "/" ever shows the consent screen again

  @landing-discovering
  Scenario: While the first workspace discovery is still running, show progress
    Given a signed-in user who has answered the consent question
    And no workspaces are known yet
    And the initial workspace discovery has not finished
    When they visit "/"
    Then they see a "Discovering agents" progress page that refreshes itself

  @landing-empty
  Scenario: With no workspaces, the home page is the create form
    Given a signed-in user who has answered the consent question
    And the initial workspace discovery finished without finding any workspace
    When they visit "/"
    Then they see the new-workspace form

  @landing-deep-link
  Scenario: A deep link pre-fills the create form
    Given a signed-in user with no workspaces
    When they visit "/" with a git URL and/or branch in the query string
    Then the new-workspace form is pre-filled with those values
    And the form opens with its advanced fields visible

  @landing-list
  Scenario: With workspaces, the home page lists every one of them
    Given a signed-in user with one or more workspaces
      (discovered locally or synced from their other devices)
    When they visit "/"
    Then every one of those workspaces is listed
```

```gherkin
Feature: Post-sign-in destination
  Account sign-in flows (out of scope here) all funnel through "/post-login",
  which decides where a just-signed-in user lands.

  @post-login-signed-out
  Scenario: Signed-out arrivals are sent to sign in
    Given the user is not signed in
    When they arrive at "/post-login"
    Then they are redirected to the sign-in prompt

  @post-login-consent-first
  Scenario: The unanswered consent question overrides every other destination
    Given a signed-in user who has not answered the consent question
    When they arrive at "/post-login", with or without a return destination
    Then they are redirected to "/", where the consent screen is shown

  @post-login-return-to
  Scenario: A safe return destination wins
    Given a signed-in user who has answered the consent question
    When they arrive at "/post-login" with a return destination that is a path on this origin
    Then they are redirected to that path

  @post-login-defaults
  Scenario Outline: Otherwise, the destination depends on whether any workspace exists
    Given a signed-in user who has answered the consent question
    And no return destination (or one that was rejected as unsafe)
    And they have <workspaces>
    When they arrive at "/post-login"
    Then they are redirected to <destination>

    Examples:
      | workspaces              | destination                        |
      | at least one workspace  | the account-management page        |
      | no workspaces           | "/" (which shows the create form)  |
```

```gherkin
Feature: One sign-in opens every workspace
  Each workspace is served on its own origin. The bare-origin session is
  bridged to each workspace origin automatically: the user signs in once,
  ever, per browser.

  @workspace-open
  Scenario: Opening a workspace from the landing page needs no second sign-in
    Given a signed-in user with a workspace
    When they open that workspace from the landing page
    Then the workspace UI loads
    And they are not asked to sign in again

  @workspace-direct-nav
  Scenario: Direct navigation to a workspace origin heals a missing workspace session
    Given a user who is signed in on the bare origin
    But whose browser has no (or a stale) session for a workspace's own origin
    When they navigate directly to that workspace's address
    Then they end up in the workspace UI without being asked to sign in

  @workspace-signed-out
  Scenario: A fully signed-out visitor to a workspace address ends at the sign-in prompt
    Given a browser with no session of any kind
    When it navigates to a workspace address
    Then it is redirected to the bare origin's sign-in prompt

  @workspace-non-html
  Scenario: Signed-out programmatic requests to a workspace are refused outright
    Given a request with no session that does not accept HTML (an API call, an asset fetch)
    When it reaches a workspace address
    Then it is refused (HTTP 403) with no redirect
```

## Invariants

These properties hold across all scenarios, all routes, and all interleavings of
requests -- including ones no scenario above describes.

### INV-1: A one-time code grants at most one session, ever

Over its whole lifetime, each one-time code is spent at most once, and only by
the authentication step that establishes a session. Every later presentation of
the same code is refused. No sequence or interleaving of requests can spend a
code twice or sign in twice from one code.

Rationale: the login URL is written in plain text to a terminal and an event
stream; single-use bounds the damage of that exposure.

### INV-2: No user data without a session

No request without a valid session may ever observe user data: workspace names
or ids, account details, settings, or any per-user content. The only things an
unauthenticated request may receive are the sign-in machinery itself
(`/login`, `/authenticate`), a sign-in prompt, a redirect toward sign-in, an
authentication refusal, or an inert application shell with no data in it.

Note: today the *shape* of refusal varies by surface (an HTTP 403, a redirect,
a placeholder page, an `auth_required` event on a stream). The invariant is the
absence of data, not the refusal shape. Unifying the shapes would be a
reasonable future tightening of this spec.

### INV-3: Sessions are unforgeable, tamper-evident, and bounded

Only session tokens issued by this installation are accepted. Any alteration of
a token invalidates it. Tokens issued by another installation (another data
directory) are invalid here. Tokens older than 30 days are invalid.

### INV-4: The signing identity is minted once and never silently replaced

An installation mints its session-signing identity once, on first need.
Concurrent first uses agree on a single identity. A corrupted or unreadable
identity is a hard startup failure -- it is never silently re-minted, because
that would invalidate every live session without explanation.

Consequence: valid sessions keep working across restarts (`@session-survives-restart`).

### INV-5: User-supplied destinations never leave the origin

Every redirect destination that arrives from the outside (the `return_to`
parameter on `/post-login` and the account pages, the `next` parameter on the
goto bridge) is honored only when it is a root-relative path on the same origin
-- a single leading `/`, no scheme, no host, and not a protocol-relative form
(`//host`, `/\host`). Anything else is ignored and the default destination is
used. No open redirects.

### INV-6: The session is the only credential the user ever handles

One sign-in grants access to every workspace the user has, current and future.
No flow ever asks the user for a second, per-workspace credential.

### INV-7: Merely fetching a URL never spends a code

Spending a code requires executing the sign-in page's script. Any URL the
system hands out (the printed login URL, links in rendered pages) is inert
under plain fetching, so preloaders, unfurlers, and prerenderers cannot consume
a code on the user's behalf.

### INV-8: The session credential never reaches workspace code

The session cookie is stripped from requests before they are forwarded to a
workspace's own server. Code running inside a workspace never observes the
credential that guards all the other workspaces.

## Traceability

Trace links are many-to-many: one scenario may be verified at several layers
(unit, route, end-to-end), and one test may witness several IDs. Paths are
repo-relative; `dc/` abbreviates `apps/minds/imbue/minds/desktop_client/` and
`fw/` abbreviates `libs/mngr_forward/imbue/mngr_forward/`.

| ID | Verified by |
|---|---|
| @signin-fresh-code | `dc/test_desktop_client.py::test_authenticate_with_valid_code_sets_cookie_and_redirects`, `::test_authenticate_redirects_to_landing_page`; `dc/auth_test.py::test_add_and_validate_one_time_code`; end-to-end: `apps/minds/scripts/launch_to_msg_e2e.py` (drives the real printed login URL) |
| @signin-used-code | `dc/test_desktop_client.py::test_authenticate_code_cannot_be_reused`; `dc/auth_test.py::test_validate_rejects_already_used_code` |
| @signin-unknown-code | `dc/test_desktop_client.py::test_authenticate_with_invalid_code_returns_403`; `dc/auth_test.py::test_validate_rejects_unknown_code` |
| @signin-prefetch | `dc/test_desktop_client.py::test_login_redirects_to_authenticate_via_js` (partial: asserts the script-only redirect page, not that the code stays unspent) |
| @signin-already-signed-in | `dc/test_desktop_client.py::test_login_redirects_if_already_authenticated` (partial: does not assert the code stays unspent) |
| @signin-missing-code | `dc/test_desktop_client.py::test_login_without_one_time_code_returns_422`, `::test_authenticate_without_one_time_code_returns_422` |
| @session-survives-restart | `dc/auth_test.py::test_get_signing_key_persists_across_instances`, `::test_codes_persist_across_store_instances` (partial: key/store persistence only; no route-level restart test) |
| @session-tampered | `dc/cookie_manager_test.py::test_verify_session_cookie_returns_false_for_tampered_value`, `::test_verify_session_cookie_returns_false_for_empty_value` |
| @session-foreign | `dc/cookie_manager_test.py::test_verify_session_cookie_returns_false_for_wrong_key` |
| @session-expiry | (gap) no covering test found |
| @landing-signed-out | `dc/test_desktop_client.py::test_landing_page_shows_login_when_unauthenticated`, `::test_landing_shows_login_not_consent_when_unauthenticated` |
| @landing-consent-gate | `dc/test_desktop_client.py::test_landing_shows_consent_screen_after_login_when_unanswered`, `::test_consent_submit_records_choices_and_unblocks_landing` |
| @landing-discovering | `dc/test_desktop_client.py::test_landing_page_shows_discovering_when_initial_discovery_not_done` |
| @landing-empty | `dc/test_desktop_client.py::test_landing_page_shows_create_form_after_discovery_finds_no_agents` |
| @landing-deep-link | `dc/test_desktop_client.py::test_landing_page_prefills_git_url_from_query_param` (partial: git URL only; branch and advanced-mode visibility unasserted) |
| @landing-list | `dc/test_desktop_client.py::test_landing_page_lists_single_agent`, `::test_landing_page_lists_agents_when_multiple_known`, `::test_mngr_cli_resolver_landing_page_lists_single_discovered_agent` |
| @post-login-signed-out | `dc/test_desktop_client.py::test_post_login_redirects_to_login_when_unauthenticated` |
| @post-login-consent-first | `dc/test_desktop_client.py::test_post_login_routes_to_landing_while_consent_unanswered` |
| @post-login-return-to | `dc/test_desktop_client.py::test_post_login_honors_safe_return_to` |
| @post-login-defaults | `dc/test_desktop_client.py::test_post_login_redirects_to_accounts_when_workspaces_exist`, `::test_post_login_redirects_to_create_when_no_workspaces`, `::test_post_login_ignores_unsafe_return_to` |
| @workspace-open | `fw/server_test.py::test_goto_authenticated_redirects_to_subdomain_with_token`, `::test_http2_goto_authenticated_redirects_to_https_subdomain`, `::test_http2_subdomain_auth_bridge_sets_secure_cookie`; end-to-end: `apps/minds/scripts/launch_to_msg_e2e.py` |
| @workspace-direct-nav | `fw/server_test.py::test_subdomain_unauthenticated_html_redirects_to_goto_bridge`, `::test_http2_subdomain_unauthenticated_html_redirects_to_https_goto` |
| @workspace-signed-out | `fw/server_test.py::test_goto_unauthenticated_redirects_to_root` (partial: covers the bridge hop; the full chain from workspace address to sign-in prompt is unasserted) |
| @workspace-non-html | `fw/server_test.py::test_subdomain_unauthenticated_non_html_returns_403` |
| INV-1 | `dc/auth_test.py::test_validate_rejects_already_used_code`, `::test_add_and_validate_one_time_code`; `dc/test_desktop_client.py::test_authenticate_code_cannot_be_reused` (no concurrency/interleaving test) |
| INV-2 | `dc/test_desktop_client.py::test_accounts_page_requires_auth`, `::test_settings_page_requires_auth`, `::test_workspace_settings_page_requires_auth`, `::test_inbox_requires_auth`, `::test_create_page_rejects_unauthenticated`, `::test_creating_page_rejects_unauthenticated`, `::test_consent_page_requires_auth`, `::test_consent_submit_requires_auth`, `::test_chrome_events_sse_returns_auth_required_when_unauthenticated`, `::test_chrome_page_renders_without_auth` (the inert-shell exception); `dc/mind_controls_test.py::test_stop_mind_hosts_requires_authentication`, `::test_running_minds_requires_authentication`, `::test_stop_state_container_requires_authentication`; `dc/api_v1_test.py::test_list_workspaces_requires_bearer`, `::test_list_workspaces_accepts_session_cookie`, and the other `*_requires_bearer` tests there. Enumerated per-surface today; no exhaustive route sweep exists. |
| INV-3 | `dc/cookie_manager_test.py` (all of it); expiry bound is a gap (see @session-expiry) |
| INV-4 | `dc/auth_test.py::test_get_signing_key_is_consistent_under_concurrent_first_access`, `::test_get_signing_key_raises_for_empty_key_file`, `::test_get_signing_key_raises_on_read_error`, `::test_get_signing_key_raises_on_write_error`, `::test_get_signing_key_reads_existing_key`, `::test_signing_key_file_has_restricted_permissions` |
| INV-5 | `dc/responses_test.py::test_safe_local_redirect_path_accepts_same_origin_paths`, `::test_safe_local_redirect_path_rejects_unsafe_values`; `dc/test_desktop_client.py::test_post_login_honors_safe_return_to`, `::test_post_login_ignores_unsafe_return_to`, `::test_auth_page_ignores_unsafe_return_to`; `fw/server_test.py::test_goto_rejects_protocol_relative_next`, `::test_subdomain_auth_bridge_rejects_protocol_relative_next` |
| INV-6 | witnessed indirectly by @workspace-open / @workspace-direct-nav; no dedicated test |
| INV-7 | (gap) partially witnessed by `dc/test_desktop_client.py::test_login_redirects_to_authenticate_via_js`; no test fetches the login URL and asserts the code is still spendable |
| INV-8 | `fw/server_test.py::test_subdomain_forward_strips_session_cookie_before_proxying_to_backend`, `::test_subdomain_forward_strips_session_cookie_when_only_session_cookie_present` |

### Notes from authoring

- Writing this spec surfaced documentation drift, fixed alongside it:
  `dc/README.md` claimed the session cookie was issued with `Domain=localhost`
  and that the landing page redirected straight to a sole agent, and a
  test-helper docstring repeated the cookie claim. All described older
  behavior (the cookie is host-only, with subdomain access provided by the
  goto bridge, and the landing page lists workspaces even when there is
  exactly one).
- `SKIP_AUTH=1` (an environment variable) bypasses every session check. It is a
  development escape hatch, intentionally unspecified here, and has no covering
  test.
