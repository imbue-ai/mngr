This is the primary flow for how a user would create a workspace for the first time:

1. User starts the desktop client. The loading screen plays the intro on the install's first launch (the lockup, two typed lines, the lockup parking in the titlebar) while the backend comes up.
2. Once the backend is ready and the install has never been taken past onboarding, the app lands on the start flow: a chat that asks "Wait.. what is honest software?", answers itself, and offers "Sounds great, let's continue".
3. The chat asks where the first workspace should run: Imbue Cloud (recommended) or Custom, with "I already have one (log in)" for a returning user, who signs in and lands on their workspace list.
4. Imbue Cloud asks for an Imbue account (sign-up or sign-in in the system browser) unless one is signed in, then creates the workspace with the remote preset. Custom opens the full create form (name, compute, backups, region, repository, branch) as a modal.
5. Submitting a create lands on the creation page, in the normal app frame: the settings the user chose as their own message, "Setting up your workspace", reading material behind chevron toggles for the wait, and a loading box with the progress bar, the stage caption, and the expandable log. The desktop client clones the repository (if a URL) and runs `mngr create system-services@<host> --new-host --no-connect --label workspace_display_name=<name> --label is_primary=true --template main --template <mode>` (the agent id is read back from the `created` JSONL event rather than pre-generated).
6. When creation completes, "Your workspace is ready! What would you like to do first?" appears with a numbered list of ways to start, the conversation so far is handed to the workspace as its first chat, and the workspace's color washes over the window, revealing the workspace's own interface open on that chat. The user's first message there picks the provider account and starts the workspace's first agent.

For subsequent launches:
- The loading screen opens on the parked mark and status line; no intro plays.
- If the user has workspaces (or has completed onboarding), they land on the home page listing every workspace, or on their restored windows.

Creating additional workspaces:
- Users press Create on the home page, fill in the form, and land on the same creation page without the preceding conversation.
- Programmatic creation is available via `POST /api/v1/workspaces`, polling `GET /api/v1/workspaces/operations/create/{operation_id}` for progress.

The point of this whole flow is to make it as easy as possible for users to get a workspace running.
