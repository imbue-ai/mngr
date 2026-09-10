In dev mode, the Electron shell now honors an existing `MINDS_LATCHKEY_BINARY` instead of always overwriting it with the bundled copy, so the app can be run against a latchkey checkout for testing an unreleased latchkey. The path reaches every latchkey subprocess minds spawns, the gateway included. A packaged build still ignores the variable, mirroring how `MINDS_ROOT_NAME` is handled, so a stale export from a parent shell cannot redirect a shipped binary.

Agents can now ask to store credentials for a third-party service Minds does not ship, securely. The agent names a domain and, optionally, that the service signs in by cookie; the request arrives in the same permission popup as every other kind, and one Approve creates the connection and grants the asking machine access to it.

The dialog is the simplest of the kinds. It shows the domain, what approving does, the sign-in URL when there is one, and the agent's reason. There is no account picker -- the service is being created, so it has none to choose between -- and no permission editor, since the grant is all-or-nothing. A service with no browser sign-in still needs credentials, and asks for them through the same form an AWS or Coolify connection uses -- but one step later: the service does not exist until Approve registers it, so it has no credential command to build inputs from until then. The first Approve registers and comes back asking; the second supplies the values.

The agent supplies no display text at all: a custom service is labelled by its origin everywhere it is named, so it cannot be presented as something it is not. Once created it behaves like any other connection, appearing in the Permissions tab and on the Connectors page, where its access can be revoked. Services with no brand mark now show a globe rather than a box.


A custom service approved mid-session now appears immediately in the Permissions tab and on the Connectors page. The services catalog held its first read for the life of the process, so until minds was restarted the connection worked while every surface that names services claimed it did not exist.

The custom-service dialog shows the origin the connection will cover -- scheme and domain -- rather than the bare domain, and says outright when it is plain http, since that is the difference between credentials sent encrypted and in the clear. The messages the agent receives name the origin too.

Asking to store credentials for an origin that is already a connection on this computer no longer fails. That is what a second workspace wanting the same service sees -- its own gateway has no service for it -- so the dialog now says the connection exists, names the sign-in it actually has, and Approve connects the asking workspace to it as it is.

The custom-service dialog now warns, in one line, when the domain is a reserved name nothing answers to, a local or private name the workspace machine resolves itself, an IP address, or a punycode look-alike. Nothing is blocked; the line says what to check before approving.

The custom-service dialog is now titled "Storing credentials for <origin>", and its summary says plainly what approving does: the credentials stay in Minds' encrypted store without being exposed to the agent directly, and the agent's access can always be revoked.

A custom service is now labelled by its origin (`https://httpbin.org`) rather than its bare domain in the Permissions tab, on the Connectors page and on inbox cards, so an http and an https service on one domain no longer look identical.
