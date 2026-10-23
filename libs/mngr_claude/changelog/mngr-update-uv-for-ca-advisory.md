- mngr's Claude Code dialog registry is read off Claude Code 2.1.307 (was 2.1.293). None of the dialogs mngr answers changed.

- Fixed the LSP plugin recommendation dialog always refusing: its decline label belonged to a different dialog. It now declines with "No, not now".

- The plugin recommendation dialog ("Would you like to install it?") is now recognized and declined with "No". Before, the unknown-dialog fallback answered it "1", which installs the plugin.

- An option label now matches only at the start of an option, and only where whitespace or the option's end follows it, so "No" never selects "No, and don't show plugin installation hints again".
