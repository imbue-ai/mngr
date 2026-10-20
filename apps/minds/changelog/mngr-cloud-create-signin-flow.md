# Imbue Cloud creates sign in and verify first, the default account follows sign-outs, Manage Accounts and the color picker stay live, and reopening from the dock restores your windows

- Pressing Create on the Imbue Cloud option while signed out now opens the sign-in and, once you are signed in, creates the machine and takes you to its creation page. Previously it sent you to the Accounts page, or failed with an account error that opened the advanced configuration.

- An Imbue Cloud create from the create form now waits for the account's email to be verified. It sends the verification link and holds Create, with Resend and Cancel options, until the link is clicked. Previously an email sign-up's first create failed on the creation page with a raw `mngr create` error.

- The create form no longer pre-selects an account that has been signed out, whether it is the default account or the account of a failed create being retried. That stale default was what made the signed-out create fail with "imbue_cloud backups require a selected account".

- Signing out the default account now makes the first account still signed in the new default right away; you can change it on the Accounts page. Switching accounts (signing out of one and into another) no longer leaves the departed account as the default for new workspaces. A stored default only applies while its account is signed in; otherwise the sole signed-in account is the default, the Accounts page and the account launcher mark it as such, and signing in replaces a default whose account has signed out.

- Changing a workspace's color in Machine settings no longer gets stuck on "Saving". A picked color shows right away and the swatches stay usable while the save runs. The save writes a label on the machine, and a slow or struggling machine could hold it for up to two minutes. If you pick again during a save, your latest pick is saved next. Only a failure of that latest pick is reported, and the swatch then returns to the saved color.

- A picked workspace color now shows in the menu bar, the home page and every other window right away, every time, while the save to the machine finishes in the background. Each pick is sent the moment it is made; the machine is written one pick at a time, and a pick already replaced by a newer one is skipped rather than written. If the latest pick's save fails, the color changes back everywhere.

- Manage Accounts now updates while it is open, from the same update that relabels the bottom-left account launcher: an account added through "Add account", signed back in with "Sign in again", signed out, or made the default shows up in the list at the same moment the launcher changes, instead of the dialog staying on "No accounts logged in" or the old list until it is reopened.

- Signing in from Manage Accounts no longer ends on a "You're signed in" dialog: the sign-in dialog closes as soon as the sign-in lands, and the new account appears in the list. The sign-in dialog also now appears on top of Manage Accounts rather than hidden behind it, so "Add account" no longer sits stuck on "Opening browser…", and Escape closes that dialog first instead of closing Manage Accounts behind it.

- On macOS, reopening Mind from the dock icon (or launching it again) after closing its last window now brings back the workspace windows that were open, instead of landing on the workspace selector. `Cmd+N`, `File > New Window`, and the dock menu's New Window still open a fresh window on the home page.
