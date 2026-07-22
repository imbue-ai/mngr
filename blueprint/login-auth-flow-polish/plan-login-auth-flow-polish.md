# Better login authorization flow

Make the OAuth (Google / GitHub) sign-in feel responsive and finish cleanly: give
immediate feedback when a provider button is clicked, narrate the wait, and pull the
Minds window back to the front once the browser round-trip completes.

Scope covers both surfaces that share `AuthForm` + `static/auth.js`: the standalone
`/auth` page and the create-screen sign-in modal. The pure-CLI terminal login
(`mngr imbue_cloud auth oauth`) is out of scope, and per this round the browser-side
"Authorization successful" callback page is explicitly **not** touched — so this is a
`minds`-only change (no `mngr_imbue_cloud` edits).

## Overview

- Today, clicking Google/GitHub gives no visual change on the button itself; a single
  static "Waiting for you to finish signing in..." line appears and stays frozen until
  the flow finishes, so during the browser round-trip nothing looks like it is moving.
- After OAuth completes in the external browser, the user is left looking at the browser
  and must manually switch back to Minds; the app does not raise itself.
- Failures/timeouts surface via `alert()` popups, which are jarring and inconsistent with
  the in-page error styling used elsewhere.
- This change is purely presentational/UX in the desktop client: button feedback, staged
  wait messaging anchored to real flow signals, window focus on success, and in-page
  error handling. It does not change the auth protocol, the callback listener, or the
  status API (which still reports only `running` / `done` / `error`).
- All edits live under the `minds` project (the shared login form template, `auth.js`,
  and the Electron main/preload window-focus surface).

## Expected behavior

- Clicking a provider button immediately shows a small spinner in place of that provider's
  brand icon, and both provider buttons dim slightly and become non-interactive.
- The waiting message becomes a short staged sequence tied to real events rather than one
  frozen line:
  - on click: an "opening your browser" message,
  - once the flow has actually started: a "waiting for you to finish with Google/GitHub"
    message (provider-named),
  - when the status poll flips to `done`: a brief "signing you in" message just before the
    page navigates onward.
- When the flow completes successfully, the Minds window raises and takes focus if it is
  currently in the background; if it is already focused, nothing jumps. (No detection of
  which external app is frontmost — an unfocused window is simply brought forward.)
- On failure, timeout, or a lost flow, the error is shown in the same in-page accent/error
  box instead of an `alert()`, the spinner stops, the buttons un-dim and become clickable
  again, and the staged message is cleared so the user can retry.
- Behavior is identical whether the login form is shown as the standalone `/auth` page or
  as the create-screen sign-in modal, since both share the same form and script.
- Password (email) sign-in and sign-up are unchanged.

## Changes

- **Provider buttons:** give each OAuth button a spinner slot in front of its brand glyph
  that activates on click (replacing the icon on the clicked provider), and apply a slight
  dimmed/disabled treatment to both buttons for the duration of the flow. Reset on
  success-navigation or on failure.
- **Staged wait messaging:** replace the single static waiting line with a small set of
  messages advanced by the events the client already observes — button click, flow started
  (flow id returned), and status `done` — keeping the existing accent ("blue box") styling.
- **Window focus on success:** when the status poll reaches `done`, ask the Electron main
  process to bring the app window forward and focus it only if it is not already focused;
  wire this through the existing renderer→main bridge used for other window actions.
- **Failure/timeout handling:** convert the OAuth failure, timeout, and lost-flow paths
  from `alert()` popups to the in-page error box, and have each of those paths also reverse
  the button spinner/dim state and clear the staged message.
- **Out of scope (this round):** the browser-side callback/"Authorization successful" page,
  its copy and branding, and any `mngr_imbue_cloud` CLI changes; the OAuth status API and
  the callback listener are untouched.
- **Changelog:** one entry under the `minds` project describing the login feedback,
  window-focus, and error-handling improvements.

## Open questions

- Exact wording of the staged messages (deferred: user picks final copy; the plan only
  fixes the number and trigger points of the steps).
- Whether the slight button "fade" should be a fixed opacity step or reuse an existing
  disabled-state token from the design system — a visual-consistency detail to settle at
  build time.
- Focus behavior nuance on macOS: raising an unfocused background window may still be
  subject to OS focus-stealing rules; if a hard focus is blocked in practice, a dock-bounce
  fallback can be added, but this round assumes a plain raise+focus is sufficient.
