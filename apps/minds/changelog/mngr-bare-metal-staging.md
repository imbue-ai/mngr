imbue_cloud workspace creation now sends the form's repository to the lease, so the fast path can only adopt a pre-baked host that genuinely matches the requested repo (previously the repo was dropped and only an operator-chosen branch label was matched, so a request for one repo could silently adopt a host running another).

- The desktop client passes the create form's repository through as `-b repo_url=<repository>` (a remote URL in production, a local clone path in dev); the imbue_cloud provider canonicalizes it (resolving a local path to its `origin` remote). The client does no git logic itself.

- `minds pool create` (the OVH pool bake wrapper) now takes the bake source as exactly one of `--from-tag <tag>` (production) or `--workspace-dir <dir>` (dev) and derives the stamped identity from it; `--attributes` is optional and must not carry `repo_url` / `repo_branch_or_tag`.
