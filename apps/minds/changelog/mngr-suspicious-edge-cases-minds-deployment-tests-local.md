Hardened suspicious edge-case handling in the minds `deployment_tests` helper modules:

- The stale-test-user sweep no longer treats a SuperTokens user with a missing `timeJoined` as
  infinitely old (which could have authorized deleting a concurrent run's fresh user); unknown
  age now biases toward skipping.
- `neon_project_exists` no longer coerces a missing `projects` key to an empty list, so a
  malformed Neon response surfaces loudly instead of silently reporting "project does not exist".
- `supertokens_app_exists` no longer infers "app does not exist" from a generic `not found`
  substring; only the specific SuperTokens error string and 401/404 status codes count as absence.
- Removed dead `subject`/`created_at` fields and the unused `_parse_iso_timestamp` epoch-fallback
  helper from the mail.tm client.
- Documented the remaining intentional edge-case handling (SuperTokens response-shape tolerance,
  empty mail.tm body fall-through, malformed-message skipping, and the `FctTemplateRef`
  at-least-one-form invariant) with clarifying comments.
