Plainer, actionable messages when creating a workspace, in the start chat, and when signing in or managing accounts. Each says what failed, adds the cause when Imbue Studio knows it, keeps the raw error behind "See details", and offers Report a bug unless it is something the user fixes themselves:

- A failed create is drawn with the chat's own pieces rather than the tinted box the rest of the app uses: the alert mark leads the sentence as an agent turn, "See details" trails it, and the ways forward are the user's answer row underneath -- the surest fix emphasized at its right, Dismiss and Report a bug as quiet links at its left. The step each sentence asks for is set apart from the rest of it, and a command it names is set as code.

- A failed create reads "Couldn't create <name>." with a cause sentence when recognised: a repository it couldn't get (worded for GitHub, other hosts and SSH), a missing branch or tag, a branch it couldn't switch to, a missing local folder, an unresolved provider or account, an unreachable provider (with the provider's own fix, e.g. "start Docker"), a name already taken, an unverified or suspended account, an app too old for Imbue Cloud, no room on Imbue Cloud, the waitlist, a named plan limit, an unknown outcome, or an unreadable template. The create log is behind "See details", also for a failure watched live.

- Retry is offered only where it can help. Imbue Cloud refusals offer "Run on this computer" (turning Imbue Cloud backups and sharing off where the account can't use Imbue Cloud), and an app too old also offers "Update Imbue Studio". These sit in the failure's answer row, the surest fix first (Update Imbue Studio, then Run on this computer, then Retry), with "Dismiss" among the row's quiet links. A Retry that can't be used (its connection was removed) is greyed out with the reason on hover, and the sentence gives that reason instead of asking for a retry; an interrupted create reads as a warning rather than a failure, and in that state leads with Discard.

- Discarding an interrupted create stays on the page until the leftover is removed, then the row leaves the workspaces list; a failed discard says so.

- The create form and Create from Template word the server's refusals plainly, show one naming a form field under that field as a small red line with the same mark, refuse up front a create under an account signed out since the form loaded, offer Sign in for a create refused for want of a signed-in account, and offer Restart Imbue Studio for a create that got no answer or a lapsed window login. While such a refusal (or a failed AI-key mint or account action) offers its fix, the page's own Create, Create key or Add account button steps down to secondary.

- The start chat says "I couldn't set that up: <reason>." and names an offered account that is no longer signed in.

- Account cards say why the plan, usage or notification settings couldn't load, with Try again, Sign in, Update Imbue Studio or Restart Imbue Studio. Manage Accounts names the action that failed ("Couldn't log out <email>."), and a plan switch Imbue Cloud refuses gives its reason without asking for a retry. A failed AI-key mint says so in our words.

- The Sign in to Imbue dialog uses our own sentences, and drops Try again when the sign-in went through but couldn't be saved on this computer. The Sign in with Imbue key page gets Try again, and "Open workspace settings" (its Account section) for a workspace with no Imbue account. The window's login page offers "Restart Imbue Studio" instead of terminal instructions.

- The providers panel reports connection failures in the styled error dialog instead of a native alert, each titled by what failed ("Couldn't remove team modal") with the cause under that title, says how many workspaces still use a connection being removed, and offers Update Imbue Studio for one a newer version added.

- The provider sign-in relay page tells an unreachable workspace from one that couldn't take the sign-in, and says when only the workspace's owner can connect an account.
