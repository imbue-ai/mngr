Combines the waitlist and signup-code work from `mngr/release-onboarding` with the browser sign-in handoff fixes from `mngr/signin-handoff-followup` (PR #1371); see this project's `mngr-release-onboarding.md` and `mngr-signin-handoff-followup.md` entries (where present) for the details of each.

- The next-deploy checklist's device-login-attempts item now names migration 052.

- When Imbue Cloud refuses a workspace create on a quota (the waitlist message for a guest, or "N of M used"), the desktop app now shows just that sentence instead of the whole `mngr create` transcript; the transcript stays in the error log record.
