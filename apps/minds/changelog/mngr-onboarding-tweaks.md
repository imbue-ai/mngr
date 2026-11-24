- Every workspace create shows one creation page: a heading ("Welcome, \<name\>!" for an account's first workspace, "Setting up \<workspace name\>" for a later one), the build's progress, and Continue once the workspace is ready. The chat-style creation page, its "honest software" exchange, the example recording, and the "Show onboarding examples?" switch are gone, and every new workspace opens on its AI-provider page and first conversation.

- A failed create's Retry and "Run on this computer" open the create form on the welcome page (they did nothing there), and reloading the creation page keeps the same page.

- An Imbue account is signed in only once its email is verified. The app no longer waits on a verification link anywhere (the first run, the create form, the template stepper, plan switches, sharing); a sign-in for an unverified address says so and offers Try again.

- The first run decides at once whether a signed-in account gets a first workspace, from the workspace count the sign-in reports, in place of waiting up to 15 seconds on the first record sync.

- The first-run pages keep their light colors whatever theme the app wears.

- `MINDS_DEFAULT_WORKSPACE_NAME_BASE` sets the base of the name given to a workspace created with none. The smoke journey uses it, and now goes through the first run as a user does: Sign in, the workspace the app starts by itself, Continue, and the AI-provider page. The next-deploy checklist says that journey fits only builds cut after 0.10.6.
