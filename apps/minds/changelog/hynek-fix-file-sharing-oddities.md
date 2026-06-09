Fixed a bug that broke WebDAV file sharing for macOS users. The `/api/v1/files`
WebDAV server shares the user's home directory, but on macOS that path
(`/Users/<name>`) contains uppercase characters. WsgiDAV matches request paths
against a lowercased copy of each share key yet looks the matched share back up
by that lowercased string, so any share key with uppercase characters resolved
to no provider and every request under it returned `404 Not Found: Could not
find resource provider`. The share is now registered under a lowercased key
(while the filesystem provider keeps the real, correct-case path), so home-
directory paths under macOS resolve correctly. Linux users were unaffected
because `/home/<name>` and `/tmp` are already lowercase.

Added the ability to change the shared path in the file-sharing permission
dialog before approving. The agent-requested path is now shown in an editable
field; you can paste a different absolute path or pick one with new
"Choose file…" / "Choose folder…" buttons that open a native OS file dialog
(separate file and folder pickers because a single combined picker can't select
both on Linux/Windows). Approving with an
edited path retargets the grant to your chosen path -- the access mode the agent
asked for (read-only vs. read & write) is preserved, and the edited path is
re-validated for traversal before any grant is written. The buttons appear only
in the desktop app (they use a native picker); in a plain browser you can still
paste a path.

The edited path is also validated against the WebDAV mount roots (your home
directory and the system temp directory) directly in Minds, so a path outside
those is rejected immediately with a clear message instead of being forwarded to
the gateway. The dialog gives instant feedback too: Approve stays disabled (and a
hint appears) while the path field is empty or points outside a shared folder, as
you type or pick.
