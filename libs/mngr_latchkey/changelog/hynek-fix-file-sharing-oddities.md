The `permission-requests` gateway extension's approve endpoint
(`POST /permission-requests/approve/<id>`) now accepts an optional JSON body
carrying a single `path` field. When present, and only for `file-sharing`
requests, the file-sharing effect is recomputed for that path instead of using
the one precomputed at request-creation time -- this lets the Minds desktop
client honor a user who edited the shared path in the approval dialog. The
overridden path is re-validated with the same traversal-rejection rules used at
request creation, the access mode fixed at creation time is preserved (a path
override cannot escalate read-only to read-write), and only the `path` field is
accepted in the body. An empty or `null` body preserves the previous behavior
(apply the precomputed effect verbatim).

File-sharing requests are now validated to be within one of the Minds WebDAV
mount roots -- the user's home directory or the system temp directory -- at
request-creation time (and at approve time for a user-edited path override).
A grant for any path outside those roots is inert (the WebDAV server has no
provider for it and answers 404), so rejecting it up front gives the agent a
clear "must be within a shared root" error instead of an approve-then-404 dead
end. The roots are derived from the gateway process's `homedir()` / `tmpdir()`,
which match the `Path.home()` / `tempfile.gettempdir()` roots the Minds WebDAV
server serves on the desktop host. The comparison is case-insensitive (mirroring
the WebDAV share-prefix matching) and purely lexical (no symlink resolution or
existence check).
