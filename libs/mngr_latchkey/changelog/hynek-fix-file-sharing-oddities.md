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
