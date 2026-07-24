# Minds authentication corpus: witness audit (Phase 1)

This document records the Phase 1 audit of the `apps/minds/specs/authentication`
behavioral-spec corpus (32 units) against the pre-existing minds test suite. The
audit adds honest `@pytest.mark.witnesses(...)` markers to the existing tests
that genuinely verify each unit (fully or partially). The corpus itself is
read-only and was not touched.

## Before / after coverage (`mngr specs matrix --root apps/minds/specs`)

| Coverage | Before audit | After audit |
|----------|-------------:|------------:|
| full     |            0 |           5 |
| partial  |            0 |          17 |
| none     |           32 |          10 |
| **total**|         **32** |        **32** |

Both matrix runs exit 0 with no broken links.

### Full (5)

A single existing test verifies the whole unit:

| Coordinate | Witness |
|------------|---------|
| `authentication.discovering` | `test_landing_page_shows_discovering_when_initial_discovery_not_done` |
| `authentication.empty-shows-create-form` | `test_landing_page_shows_create_form_after_discovery_finds_no_agents` |
| `authentication.lists-workspaces` | `test_landing_page_lists_agents_when_multiple_known` |
| `authentication.signed-out-arrival` | `test_post_login_redirects_to_login_when_unauthenticated` |
| `authentication.safe-return-to` | `test_post_login_honors_safe_return_to` |

### Partial (17)

Witnessed, but with an honest `partial=` note. Two structural patterns recur and
are worth calling out because they are genuinely, behaviourally covered yet the
matrix reports them as `partial` (see "Machinery observations" below):

- Scenario outlines covered by one test per example row: `missing-code`
  (`/login` + `/authenticate`), `default-destination` (has-workspaces +
  no-workspaces).
- Compound scenarios covered by one test per When/Then pair: `consent-gate`
  (screen shown after sign-in + never shown again after answering).

The remaining partials are genuinely incomplete:

- `fresh-code` — the "opens the login URL in a browser" flow and the "code is now
  spent" step are split across tests / not asserted together; existing tests
  drive `/authenticate` directly.
- `used-code`, `unknown-code` — refusal asserted, but not the absence of a
  session cookie on the refusal.
- `already-signed-in` — redirect asserted, but not that the fresh code stays
  unspent.
- `signed-out-home` — sign-in prompt asserted, but not the "reveals nothing about
  existing workspaces" step.
- `consent-first` — only the no-return-destination case.
- `deep-link-prefill` — only `git_url` prefill; not branch prefill or advanced
  fields opening.
- `single-use-codes`, `sessions-unforgeable`, `signing-key-minted-once`,
  `no-data-without-session`, `no-open-redirects` (rules) — each is universally
  quantified; witnessed for representative flows/surfaces but not exhaustively.
- `tampered-token`, `foreign-token` — witnessed only at the `verify_session_cookie`
  unit boundary (predicate rejects), not at the HTTP "treated as signed out"
  surface. `tampered-token` uses garbage input rather than a mutated valid token.

### None (10) — targets for the Phase 2 fleet

| Coordinate | Why unwitnessed today |
|------------|-----------------------|
| `authentication.prefetch` | No test asserts the code stays spendable after a scriptless fetch of `/login`. |
| `authentication.fetch-never-spends` (rule) | Same: the inertness observable is never asserted. |
| `authentication.survives-restart` | Only the signing-key persistence *mechanism* is tested, never a session surviving a client restart at the HTTP level. |
| `authentication.expired-token` | The 30-day `max_age` is entirely unexercised at any level. |
| `authentication.open-from-landing` | Witnessed only in `libs/mngr_forward` (see below). |
| `authentication.direct-navigation` | Witnessed only in `libs/mngr_forward`. |
| `authentication.signed-out-workspace` | Witnessed only in `libs/mngr_forward`. |
| `authentication.non-html-refused` | Witnessed only in `libs/mngr_forward`. |
| `authentication.single-credential` (rule) | Witnessed only in `libs/mngr_forward`. |
| `authentication.credential-not-forwarded` (rule) | Witnessed only in `libs/mngr_forward`. |

## Key finding: the workspace bridge is witnessed in the wrong project for the matrix

The corpus overview declares the workspace-origin bridge to be served by the
`mngr_forward` plugin (`libs/mngr_forward/`), and all six workspace-bridge units
(`open-from-landing`, `direct-navigation`, `signed-out-workspace`,
`non-html-refused`, plus the `single-credential` and `credential-not-forwarded`
rules) are, in fact, thoroughly tested there — e.g.
`server_test.py::test_subdomain_forward_strips_session_cookie_before_proxying_to_backend`
(a clean FULL witness of `credential-not-forwarded`) and
`server_test.py::test_subdomain_unauthenticated_non_html_returns_403` (a clean
witness of `non-html-refused`).

But the default `mngr specs matrix --root apps/minds/specs` scans only the
corpus root's parent (`apps/minds`) for `witnesses` markers, and the
`tmr-specs-minds` recipe inherits that default test root. So markers placed in
`libs/mngr_forward` would not be counted, and these six units read as `none`
under the default matrix even though the behaviour is well covered. This is an
open decision for Phase 2, raised with Danver:

- Option A: add the `witnesses` markers in `libs/mngr_forward` tests and invoke
  the matrix (and the recipe) with `--tests apps/minds --tests libs/mngr_forward`.
- Option B: leave the bridge units to the fleet, which would need `apps/minds`-side
  integration coverage of the forward server (may require standing it up).
- Option C: treat as a corpus/ownership escalation.

No markers were added to `libs/mngr_forward` in this audit pending that decision.

## Machinery observations (for the Phase 3 reflection)

- `compute_spec_coverage` marks a unit `full` only when some single marker omits
  `partial=`. A scenario outline or compound scenario that is genuinely covered by
  a *set* of per-row / per-clause tests therefore reads as `partial`. This is by
  design (a single test is expected to stand as the full witness), but it means
  the matrix understates coverage for `missing-code`, `default-destination`, and
  `consent-gate`.
- Parametrized witnesses expand to one matrix witness per param node (the
  `no-open-redirects` guard shows 14 witnesses from 4 markers because the two
  `responses_test` parametrizations contribute 4 + 8 nodes).

## Per-unit witness map

All markers are `@pytest.mark.witnesses("<coordinate>", partial="...")` on the
listed test node, in the `apps/minds` tree.

- `fresh-code`: `test_authenticate_with_valid_code_sets_cookie_and_redirects`,
  `test_authenticate_redirects_to_landing_page` (both partial)
- `used-code`: `test_authenticate_code_cannot_be_reused`,
  `auth_test.py::test_validate_rejects_already_used_code` (both partial)
- `unknown-code`: `test_authenticate_with_invalid_code_returns_403`,
  `auth_test.py::test_validate_rejects_unknown_code` (both partial)
- `already-signed-in`: `test_login_redirects_if_already_authenticated` (partial)
- `missing-code`: `test_login_without_one_time_code_returns_422`,
  `test_authenticate_without_one_time_code_returns_422` (one per outline row)
- `single-use-codes`: `test_authenticate_code_cannot_be_reused`,
  `auth_test.py::test_validate_rejects_already_used_code` (both partial)
- `signed-out-arrival`: `test_post_login_redirects_to_login_when_unauthenticated` (full)
- `consent-first`: `test_post_login_routes_to_landing_while_consent_unanswered` (partial)
- `safe-return-to`: `test_post_login_honors_safe_return_to` (full)
- `default-destination`: `test_post_login_redirects_to_create_when_no_workspaces`,
  `test_post_login_redirects_to_accounts_when_workspaces_exist` (one per outline row)
- `no-open-redirects`: `test_post_login_ignores_unsafe_return_to`,
  `test_auth_page_ignores_unsafe_return_to`,
  `responses_test.py::test_safe_local_redirect_path_rejects_unsafe_values`,
  `responses_test.py::test_safe_local_redirect_path_accepts_same_origin_paths` (all partial)
- `signed-out-home`: `test_landing_page_shows_login_when_unauthenticated` (partial)
- `consent-gate`: `test_landing_shows_consent_screen_after_login_when_unanswered`,
  `test_consent_submit_records_choices_and_unblocks_landing` (one per When/Then pair)
- `discovering`: `test_landing_page_shows_discovering_when_initial_discovery_not_done` (full)
- `empty-shows-create-form`: `test_landing_page_shows_create_form_after_discovery_finds_no_agents` (full)
- `deep-link-prefill`: `test_landing_page_prefills_git_url_from_query_param` (partial)
- `lists-workspaces`: `test_landing_page_lists_agents_when_multiple_known` (full)
- `no-data-without-session`: `test_create_page_rejects_unauthenticated`,
  `test_accounts_page_requires_auth`,
  `test_chrome_events_sse_returns_auth_required_when_unauthenticated` (all partial)
- `tampered-token`: `cookie_manager_test.py::test_verify_session_cookie_returns_false_for_tampered_value` (partial)
- `foreign-token`: `cookie_manager_test.py::test_verify_session_cookie_returns_false_for_wrong_key` (partial)
- `sessions-unforgeable`: `cookie_manager_test.py::test_create_and_verify_session_cookie_round_trip`,
  `..._returns_false_for_wrong_key`, `..._returns_false_for_tampered_value` (all partial)
- `signing-key-minted-once`: `auth_test.py::test_get_signing_key_returns_same_key_on_subsequent_access`,
  `..._is_consistent_under_concurrent_first_access`, `..._persists_across_instances`,
  `..._raises_for_empty_key_file`, `..._raises_on_read_error` (all partial)
