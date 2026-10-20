New `imbue.mngr_latchkey.credential_commands` module: it parses the
`setCredentialsExample` a service reports (e.g. `latchkey auth set-nocurl aws
<access-key-id> <secret-access-key>`) into argv tokens plus one named parameter
per `<placeholder>`, and builds a runnable argv from a caller-supplied value per
parameter, pinned to one latchkey account. This lets an embedder collect
credentials in its own UI instead of telling the user to run a command in a
terminal (which minds now does for services with no browser sign-in).

`Latchkey.auth_set_credentials()` runs such a command. The argv carries the
user's secrets, so it is passed to the subprocess as a list (never a shell
string) and never logged: every `latchkey auth ...` invocation now also passes a
log-safe process name so the argv cannot leak into logs or errors. Unlike the
browser flows, which wait on a human and stay untimed, storing credentials is
bounded by a 60s timeout.

`LatchkeyServiceInfo` gained an `is_browser_auth_supported` computed field --
true when latchkey advertises a `browser` auth option, or reports no auth
options at all (in which case we don't know and keep offering the browser
flow). It replaces the same check open-coded in minds.

`credential_commands.describe_credential_command_failure()` reduces a rejected
credential command's output to the part worth showing a user: it keeps the
service's explanation of what is wrong with the value and drops the usage lines
("Example: ...", "Usage: ..."), JS stack frames and the redundant "Error:"
prefix around it, capping what is left at 300 characters.
