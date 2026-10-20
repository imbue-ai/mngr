The browser sign-in to your Imbue account explains what went wrong instead of always blaming the connection.

- When a sign-in times out, when the browser's handoff back to the app can't be confirmed, or when the browser signed you in but the app couldn't finish, the sign-in dialog now says which one happened and that Try again will fix it (usually in a single click, since the browser remembers you). Before, all of these showed "We could not reach the Imbue sign-in service. Check your internet connection", even though nothing was wrong with the connection.

- A sign-in that finished in the browser but failed in the app is now logged as an error, so it reaches error reporting instead of passing unnoticed.

- Every Sign in / Add account click opens the sign-in page in your browser. If an earlier sign-in is still waiting (say you closed the dialog), the click reopens that same sign-in's page rather than starting a second one to race it, so a tab you left open still works too.

- While the dialog waits, it offers "Reopen sign-in page", for a browser tab that never opened or was closed.

- The app now keeps waiting for the browser for up to an hour, instead of ten minutes, so a slow sign-in (a long Google account chooser, two-factor, a first-time Google sign-in) no longer strands the browser on a "refused to connect" page.

- If the sign-in stops unexpectedly inside the app, the dialog shows an error with Try again, and the next Sign in click starts a fresh sign-in.
