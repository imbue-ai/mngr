The first run is now a sign-in page instead of the start chat: "Reimagine your personal computer", a Sign in button that signs in through the browser, and "Use without an account", which opens the create form on running the workspace on this computer. An account with workspaces goes to its home page; a new account's first Imbue Cloud workspace starts the moment it signs in (after email verification, as before).

- While that workspace is built, the creation page welcomes the user by name and asks "What type of studio do you want to make?" with pills to pick from (three at most) and a field for their own words. The picks go to the new workspace for the agent. When the create is done the workspace loads under the same welcome, and Continue appears only once the workspace has its provider page up, so pressing it lands there at once.

- The start chat's honest-software exchange, its error-reporting question, its where-to-run question, its "Change answer" controls and the title bar's "Already set up? Log in" are gone. A new install answers the error-reporting question on the sign-in page: a "Help improve Imbue Studio" checkbox at the foot of the page, checked to start, whose label opens what it means. The answer is recorded when Sign in or "Use without an account" is pressed.

- A dev launch on a local template worktree (`just minds-start`) creates the first workspace on this computer instead of Imbue Cloud, which cannot clone a local path.

- For developers: `MINDS_DEV_FORCE_FIRST_RUN=1` makes every sign-in on the first-run page land as an account with no workspaces, so the first run can be replayed with an account that has some. `MINDS_DEV_FIRST_RUN_ON_CLOUD=1` sends that first workspace to Imbue Cloud instead, for a pool with a slice baked from the worktree.
