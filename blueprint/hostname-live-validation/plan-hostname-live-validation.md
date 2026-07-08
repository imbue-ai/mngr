# Live hostname validation in the mind-creation advanced form

## Refined prompt

> Currently when one creates a mind using the advanced configuration area the user can fill in a hostname. That field should be (a) validated for the hostname limitations (we may or may not already have this validation in place) and (b) ideally we would validate it for availability. When the user types in a name that is already taken they should know this sooner rather than later. Currently it kicks off mind creation, quite a bit of time passes by, and only then the processs errors out.
>
> * Feedback (both format and availability) appears live as the user types, debounced, shown inline near the Name field.
> * Availability is checked instantly against the local in-memory `BackendResolver` snapshot only -- no per-keystroke provider/subprocess call.
> * The inline error message sits under the Name field; the Create button stays clickable (the rule is enforced on submit, matching the existing account-picker error pattern).
> * When a name is taken, just tell the user it's unavailable -- do not auto-suggest an alternative name.
> * Validation applies to every provider offered in the advanced form (local Docker, AWS, Imbue Cloud, ...), not just the Imbue Cloud remote path.
> * A name counts as taken only if it collides within the currently-selected provider (and account, for Imbue Cloud); the check re-evaluates when the provider/account selection changes.
> * No server-side submit guard is added; the live client-side check is the sole pre-creation safeguard.
> * No on-demand snapshot refresh is added; the existing ~10s background discovery poll is relied on, accepting the small staleness window.
> * Format error messages are friendly and specific to the failing rule (e.g. "can't start or end with a dash", "dots aren't allowed").
> * Availability data reaches the page via a debounced read-only request to a small local endpoint that reads the snapshot (Q10b).
> * A name used by a destroyed/torn-down mind is treated as available, matching the provider (Q11a).
> * The "taken" comparison is case-insensitive: "My-Mind" is flagged if "my-mind" exists (Q12b).
> * If the user clicks Create on a known-invalid/taken name, client-side JS blocks the submit and surfaces the inline error, mirroring `imbueCloudNeedsAccount()` (Q13a).

## Overview

* Today the advanced-config Name field has no client-side validation; format errors only surface after submit (full-page re-render), and a taken name isn't caught at all on the form path -- it kicks off creation and fails late with `HostNameConflictError` after git clone + provisioning.
* Goal: give the user live, inline feedback as they type -- both for hostname format rules and for whether the name is already taken -- so they never start a doomed creation.
* Format rules already exist server-side (`HostName`/`SafeName` in `libs/mngr/.../primitives.py`); the work is surfacing them live with friendly per-rule messages, not changing the rules.
* Availability reuses the existing in-memory `BackendResolver` snapshot (already kept fresh by the ~10s background discovery poll, already covering all hosts on all providers/accounts) -- read via a new lightweight local endpoint, scoped to the currently-selected provider/account, excluding destroyed minds, compared case-insensitively.
* Deliberately minimal backend surface: no server-side submit guard and no new refresh machinery; enforcement on submit is client-side JS mirroring the existing account-picker pattern, and the pre-existing late `HostNameConflictError` remains only as a rare-race backstop.

## Expected behavior

* As the user types in the Name field (advanced view), feedback updates live and debounced; an inline message appears directly under the field.
* Format violations show instantly with a specific, friendly message: e.g. can't start/end with a dash or underscore, no dots allowed, only letters/numbers/dashes/underscores.
* If the typed name matches an existing mind on the currently-selected provider (and account, for Imbue Cloud), an inline "name is already taken" message appears shortly after typing.
* The match is case-insensitive ("My-Mind" is flagged when "my-mind" exists) even though the provider itself compares exactly; this is intentionally stricter to avoid surprising near-duplicates.
* A name belonging to a destroyed/torn-down mind is treated as available (reusable), matching real provider conflict behavior.
* Changing the provider or account re-runs the availability check against the new scope, so a name free on one provider may show as taken on another and vice versa.
* An empty Name field shows no error (it is auto-named on creation, as today).
* The Create button stays visually enabled, but clicking it while the name is known-invalid or taken does nothing except surface the inline error (submission is blocked client-side), matching how the Imbue-Cloud-needs-account check behaves today.
* When the name is valid and available, creation proceeds exactly as before.
* Residual edge cases (JS disabled, or a name taken on the provider within the brief staleness window / outside the snapshot) still fall through to the existing late `HostNameConflictError`; this path is unchanged and serves as the last-resort backstop.
* The separate JSON `/api/create-agent` path (non-desktop callers) keeps its existing eager 400/409 validation; this change targets the desktop advanced form.

## Changes

* Add a read-only availability endpoint to the desktop client app (`apps/minds/imbue/minds/desktop_client/app.py`) that, given a candidate name plus the selected provider (and account), reads the `BackendResolver` snapshot and reports whether the name is taken.
  * Scope the lookup to the selected provider/account and use the active (non-destroyed) workspace set; compare case-insensitively.
* Extend the Create page (`apps/minds/imbue/minds/desktop_client/templates/pages/Create.jinja`):
  * Add an inline error element directly under the Name field, following the existing `#account-error` / `#launch-mode-account-error` pattern.
  * Add a debounced input handler on `#host_name` that runs format checks and calls the availability endpoint, then shows/clears the inline message.
  * Re-run the availability check when the provider (`#launch_mode`) or account (`#account_id`) selection changes.
  * Extend the existing `form` submit handler to `preventDefault()` and surface the inline error when the name is known-invalid or taken (alongside the current `imbueCloudNeedsAccount()` guard).
* Provide friendly, rule-specific format messages for the live check, derived from the same constraints as `HostName`/`SafeName` (alphanumeric plus `-`/`_` in the middle, no leading/trailing `-`/`_`, no dots, non-empty).
* No change to the `HostName` rules, the `/create` form POST server logic, the discovery/refresh cadence, or the provider-level conflict check.
* Add a changelog entry under `apps/minds/changelog/` for the touched project (`apps/minds`).
