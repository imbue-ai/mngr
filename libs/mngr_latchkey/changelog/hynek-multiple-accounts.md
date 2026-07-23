Support for latchkey 3.0.0's multiple-accounts-per-service model.

`Latchkey.services_info` now parses latchkey 3.0.0's per-account `credentials` object (keyed by account name, with the unnamed default account keyed by `""`) instead of the removed top-level `credentialStatus` field. The parsed accounts are exposed on `LatchkeyServiceInfo.accounts`, and a single service-level `credential_status` is derived from them (no accounts means `MISSING`; any valid account means `VALID`; otherwise `UNKNOWN` before `INVALID`), so the existing permission-grant flow keeps working unchanged.

`Latchkey.auth_clear` is now account-aware: `auth_clear(service, account=...)` clears one account and `auth_clear(service, is_all=True)` wipes every account and the prepared OAuth client (`--all`, required by 3.0.0 to also drop preparations). The internal Google OAuth fallback now clears with `--all` so a failed Minds-client preparation no longer blocks the self-setup retry.

New `Latchkey.add_account(service)` runs the browser sign-in with `LATCHKEY_EPHEMERAL_BROWSER=1` so the user lands on a fresh sign-in screen and can add a genuinely new account; for Google services it always falls back to a fresh `auth browser-prepare` when signing in with the official Minds client does not succeed.
