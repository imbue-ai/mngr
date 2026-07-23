The Settings page's Connectors tab now surfaces the signed-in accounts for each connected service.

Under each service, minds lists the accounts you've connected (read from a single `latchkey auth list --offline` call), each with a "Disconnect" action, above the existing per-workspace permission list (now labelled "Allowed on all accounts:").

A "+ Add account" button next to each service runs the same browser sign-in as approving a permission request, but in latchkey's ephemeral-browser mode so you land on a fresh sign-in screen and can add a new account rather than being re-authenticated as an existing one. For Google services, if signing in with the official Minds client does not succeed it falls back to a fresh self-setup step.

"Disconnect" clears that one account's stored credentials. Disconnecting the last account for a service also revokes that service's permissions from every workspace in the background (the grants would otherwise have no credentials behind them).
