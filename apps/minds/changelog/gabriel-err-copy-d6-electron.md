Plainer startup errors in the desktop app and the Linux install script:

- When Imbue Studio cannot start, the error screen now reads "Imbue Studio couldn't start. Try again. If this persists, report a bug." When the app started but never answered, or a step after that failed, "Show details" also carries the last lines of the app's log (where the real reason is), not just a port; the bug report sent from that screen carries them too. Neither this screen nor the "stopped unexpectedly" screen shows the one-time login code those log lines can hold.

- A failed first-launch setup no longer blames the internet for everything. It reads "Imbue Studio couldn't finish setting itself up." followed by "Check your internet connection, then press Retry." only when a download failed, "Your disk is full. Free up some space, then press Retry." for a full disk, and "Try again. If this persists, report a bug." otherwise.

- Retry on a failed setup now runs setup again before starting the app, instead of skipping straight to starting it.

- "View full log" now opens the log that holds the failure on screen (the setup's log for a setup failure), sits under "See details", and says where the log is when it cannot be opened, instead of silently doing nothing.

- Every loading screen now shows the progress bar and status line, not only the one at launch: after a Retry, a "Restart Imbue Studio", or a window opened while the app is starting.

- If moving Imbue Studio's files to their new folders fails on macOS, one box titled "Imbue Studio couldn't finish moving its files" explains that nothing was deleted and that opening the app again finishes the move, without a stack trace and without a second crash box.

- `install-linux.sh` now names the step that failed when a command it runs fails, and says that re-running the script is safe. A WSL install with no PowerShell to create the Windows desktop shortcut now says to start Imbue Studio from a WSL terminal with the command shown.

The startup error screen is drawn to the shared error design: the alert mark leads its title, the sentence under it is the page's own ink rather than a greyed line, "See details" trails that sentence (and is absent when there is nothing to disclose) with the raw output left-aligned under it and the buttons last, and the step a sentence asks for -- "*Free up some space, then press Retry*", "*Check your internet connection, then press Retry*" -- is set apart from the rest of it. The app draws this mark red; this screen has no red that reads on its brown, so it takes the page's ink, as every other shape on that page does.
