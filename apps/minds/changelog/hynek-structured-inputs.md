Permission dialogs no longer ask you to open a terminal when a service needs
credentials set by hand (AWS, Coolify, ...).

Instead of printing a `latchkey auth set-nocurl aws <access-key-id>
<secret-access-key>` command to copy-paste, the dialog now shows one labeled
input per value that command needs, at the top of the dialog, as soon as an
account that needs credentials is selected. Approving fills the values in, runs
the command inside Minds (against the account you selected and Minds' own
credential store), re-checks the credentials, and completes the grant -- one
click, no terminal. The command itself is never shown; it is not something you
need to know about.

Switching to an already-connected account hides the form again. If the
credentials are rejected, or storing them fails, the form stays with everything
you typed and explains what went wrong, so you can fix one field and retry.
Adding a *second* account of such a service also asks for a name for it, since
there is no browser sign-in to discover one.

If Minds cannot work out which credentials a service wants, the dialog says so
and offers no Approve button, leaving Deny as the only action.

Account choices no longer promise "opens a browser sign-in" for services that
have no browser sign-in; they say they will ask you for credentials instead.

On the Settings page's Connectors tab, "+ Add account" is now disabled -- with
the reason on hover -- for services that cannot be signed in to through a
browser, instead of popping an error alert after you click it. Those services
are connected from a permission dialog, which asks for their credentials
directly.

That per-service check costs one latchkey call each, so they all run at once
rather than in series -- the Connectors tab does not get slower as you connect
more services.

When a service rejects what you typed, its own explanation is shown ("The
provided access key ID doesn't look like an AWS access key ID...") without the
usage lines latchkey appends after it -- those either restate a terminal
command or, for AWS, print the placeholder itself as the "example", which is
no help next to an input already labelled that way.

Credentials that are the right shape but wrong -- mistyped, revoked, rotated or
expired -- are stored by latchkey without complaint and only fail when the
service is actually called, so Minds makes that call before granting anything
and brings the form back if it fails, rather than granting a permission the
agent could not use.

A failed attempt is now shown the way every other error in the app is -- a red
error notice (announced to screen readers) in place of the form's instruction --
and is scrolled into view, so the reason cannot end up off-screen above the
Approve button you just clicked. The same applies to an approval that fails
outright.

A connected account whose credentials stopped working is now hinted as "needs
credentials" rather than "needs sign-in" when its service has no browser
sign-in -- picking it opens the same credential form a new account does.
