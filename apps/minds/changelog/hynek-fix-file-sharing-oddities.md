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
