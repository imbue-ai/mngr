# imbue_cloud fast-path matching by repo + branch

Make the imbue_cloud **fast path** (adopt a pre-baked pool host instead of rebuilding) a *sound* optimization: a host is adopted only when the user's requested **repository AND branch/tag genuinely match what was baked into that host**; otherwise we fall back to the slow path and build exactly what the user asked for. Today the match ignores the repo entirely and only compares an operator-chosen branch label that need not reflect the host's real contents -- so a request for one thing can silently adopt a host running something else.

Audience: developers working on `mngr_imbue_cloud`, the minds desktop client, and the bare-metal/pool bake tooling.

## Motivation

The fast path is a transparent optimization: *if* a warm pool host already contains exactly what you asked for, adopt it (seconds); *otherwise* build what you specified (slow path). For that to be safe, the host's advertised identity must truthfully describe its contents, and the request must describe what the user actually wants. The current implementation breaks both halves:

- The desktop client **drops `repo_url`** from every imbue_cloud lease (passes `None`), so the repo is never matched -- a host baked from repo A can be adopted by a request for repo B.
- The baked host's `repo_branch_or_tag` is **whatever the operator hand-typed** in `--attributes`; it need not match the content that was actually baked (e.g. a slice baked from a working tree on `josh/ovh-exploration` can be labeled `main`).

Result: "specify one thing, get some other code" -- the exact failure this spec removes.

## Current state (as of this writing)

- `LeaseAttributes` (`libs/mngr_imbue_cloud/.../data_types.py`) already has `repo_url`, `repo_branch_or_tag`, `cpus`, `memory_gb`, `gpu_count`. The data model is not the blocker.
- **Request side** (`apps/minds/.../desktop_client/agent_creator.py`): for `LaunchMode.IMBUE_CLOUD` it sends `-b repo_branch_or_tag=<form branch>` and `-b region=<form region>`, but deliberately sends **no** `repo_url` (the wiring exists at the `imbue_cloud_repo_url` param but is fed `None`, because the form's repository is sometimes a local clone path that would never match a canonical URL).
- The installed/default form repository is a real remote: `https://github.com/imbue-ai/forever-claude-template.git`, default branch `v0.3.0` (a tag). A **local path only appears in dev**, via `MINDS_WORKSPACE_GIT_URL` gated behind `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` (set by `just minds-start` / e2e). `templates.py` already notes these dev defaults "must not be kept" for imbue_cloud.
- **Bake side**: a pool row's `attributes` JSONB = the operator's `--attributes` JSON (plus, for slices, auto-stamped `{memory_gb, cpus}`). `repo_url` is present only if the operator hand-included it; `repo_branch_or_tag` is whatever they typed -- never derived from or checked against the baked content.
- **Connector match** (`apps/remote_service_connector/.../app.py`): `pool_hosts.attributes @> request.attributes` (JSONB containment) plus a separate `region` column equality. Slow path uses `LeaseAttributes.relaxed()` (drops repo/branch, keeps resources).

## Design decisions (settled)

These were decided up front and are not open for re-litigation in implementation:

1. The fast-path match is on **`repo_url` + `repo_branch_or_tag` + `region`** only. Resource attributes (`cpus`/`memory_gb`) stay **informational** for now (still stamped, not matched); matching on requested resources is future work.
2. `repo_url` is compared after **canonicalization** (normalize URL forms + resolve a local path to its `origin` remote), not by raw string equality.
3. `repo_branch_or_tag` stays a **single opaque string** matched exactly (a branch `main` and a tag `v0.3.0` are just strings).
4. `repo_url` is stored in the existing `pool_hosts.attributes` JSONB -- **no new column, no migration**; the existing `@>` match covers it.
5. **No backwards compatibility.** Existing pool hosts (baked without a canonical `repo_url`) will simply be re-baked; we do not add a transition path.
6. `fast_mode=require` **hard-errors** if it cannot determine a canonical `repo_url` *and* a `repo_branch_or_tag` -- it must never silently match on a subset.
7. Production bakes build from a **clean checkout of an exact tag** (strict); dev/debug bakes may take content from an explicit working-tree folder and *label* it (best-effort).

## The contract

A pool host's `attributes` must **truthfully describe what is baked into it**:

- `repo_url`: the canonical identity of the repository the host was built from.
- `repo_branch_or_tag`: the git ref the content was built from (a tag for production, a branch for dev).
- `cpus` / `memory_gb`: the host's resources (informational for matching, for now).

A lease request describes **what the user wants**: a canonical `repo_url`, a `repo_branch_or_tag`, and (separately) a `region`.

- **Fast path** (`fast_mode=require`): adopt a host iff `host.attributes @> {repo_url, repo_branch_or_tag}` and `host.region == request.region`. If no such host exists, raise `FastPathUnavailableError` so the caller falls back. If the request lacks a canonical `repo_url` or a `repo_branch_or_tag`, raise a clear error before contacting the connector (decision 6).
- **Slow path** (`fast_mode=prevent`): unchanged -- build the user's exact spec; `relaxed()` still drops repo/branch and keeps resources so any adequately-sized host can be rebuilt.

## Canonical repository identity

A single normalization function is the heart of correctness; both sides must produce identical output for "the same repo."

**Location:** `mngr_imbue_cloud` (the provider's lease path and the bake tooling both live here and both call it). The minds desktop client must **not** reimplement it -- it shells out to `mngr` and cannot import the plugin, so it passes the form's repository through verbatim and lets the provider canonicalize.

**Inputs it accepts:** a remote URL (`https://`, `ssh://`, `git@host:org/repo(.git)`) **or** a local filesystem path.

**Algorithm:**

1. If the input is a local path (reuse the existing `_is_local_path` notion), resolve it to its `origin` remote via `git -C <path> remote get-url origin`. If there is no `origin`, raise (cannot establish identity).
2. Normalize the resulting URL to a canonical key: lowercase the host, strip the scheme / `git@` / `ssh://` / `https://`, convert `host:org/repo` to `host/org/repo`, strip a trailing `.git` and any trailing `/`. Example: all of `git@github.com:imbue-ai/forever-claude-template.git`, `https://github.com/imbue-ai/forever-claude-template`, and a local clone whose origin is either of those normalize to `github.com/imbue-ai/forever-claude-template`.

**Note:** the stored and requested `repo_url` are this canonical key, applied identically at bake time and request time so they cannot drift.

## Changes by component

### Request side (desktop client + provider)

- **Desktop client** (`agent_creator.py`): for `IMBUE_CLOUD`, stop forcing `imbue_cloud_repo_url=None`; pass the form's repository through as `-b repo_url=<repository>` (alongside the existing `-b repo_branch_or_tag` and `-b region`). No git logic added here -- the value is whatever the form holds (a remote URL in production; a local path in dev).
- **Provider** (`mngr_imbue_cloud`, where `-b` args are parsed into `LeaseAttributes` / `ParsedImbueCloudBuildArgs`): canonicalize the incoming `repo_url` (resolving a local path to its origin) before building the lease request. For `fast_mode=require`, enforce decision 6: if canonical `repo_url` or `repo_branch_or_tag` is missing/unresolvable, raise with an actionable message.

### Bake side (admin / pool tooling)

The bake derives `repo_url` + `repo_branch_or_tag` from the **bake source** and stamps the canonical values into the row's `attributes` -- operators no longer hand-type `repo_branch_or_tag` in `--attributes` (removing the class of mistake where the label diverges from the content). Two modes:

- **Production bake (strict, from a tag):** clone (or `git archive`) the canonical repo at an exact tag into a fresh temp dir and bake from that; stamp `repo_url=<canonical repo>`, `repo_branch_or_tag=<tag>`. No working-tree folder is used, so the baked content provably equals the tag. Reject anything that isn't a real tag.
- **Dev/debug bake (from a folder/branch):** take content from an explicit `--workspace-dir` (may contain uncommitted changes); stamp `repo_url=<canonical(origin of that folder)>` and `repo_branch_or_tag=<that folder's current branch>` (or an explicit override). The label is best-effort -- it identifies the branch, not a byte-exact commit.

**Flag design (locked):** the bake takes exactly one of two mutually-exclusive source selectors, and the identity attributes are always derived (never hand-passed):

- `--from-tag <tag>` -- **production** mode. Clones the repo at `--repo-url <url>` (default: the canonical FCT remote `https://github.com/imbue-ai/forever-claude-template.git`) at exactly `<tag>` into a fresh temp dir and bakes from that. Stamps `repo_url=canonical(<repo-url>)`, `repo_branch_or_tag=<tag>`. Errors if `<tag>` is not a real tag on the repo. Content provably equals the tag.
- `--workspace-dir <dir>` -- **dev/debug** mode. Bakes content from the working tree at `<dir>` (uncommitted changes included). Stamps `repo_url=canonical(origin of <dir>)` and `repo_branch_or_tag=<dir's current branch>`, overridable with `--repo-branch-or-tag <ref>`. Errors if `<dir>` has no `origin` remote (no canonical identity). The label is best-effort (identifies the branch, not a byte-exact commit).

Rules:

- Exactly one of `--from-tag` / `--workspace-dir` is required; passing both, or neither, is an error.
- `repo_url` and `repo_branch_or_tag` are **always derived** by the bake (per the mode above) and must **not** be accepted in `--attributes`; passing either key in `--attributes` is an error (it would let the label drift from the content -- the bug this spec removes).
- `--attributes` is retained only for non-identity attributes (today: none, since slice `cpus`/`memory_gb` are auto-stamped from sizing). It may be omitted; an empty/absent `--attributes` is valid.
- Slice resource sizing (`cpus`/`memory_gb`) continues to be auto-stamped from the box's per-slice sizing, in both modes.

### Storage / connector

- `repo_url` lives in `pool_hosts.attributes` JSONB (decision 4). No schema migration. The connector's `@>` containment match is unchanged in mechanism; it now naturally enforces repo + branch because requests carry them and hosts store them.

### justfile + skills

The tooling must make the cases explicit, because correctness now depends on which case you are in:

- **Bake recipes** (`bake-pool-host` / `minds pool create` wrappers): expose the dev (folder/branch) vs production (tag, strict) distinction with clearly commented recipes.
- **Fast-path test helper:** a recipe (or documented flow) that derives `repo_url` + `repo_branch_or_tag` from a single source of truth and uses it on **both** sides -- bakes a slice with those tags and launches the desktop client with the form repo/branch set to the same canonical repo + ref -- so a test cannot accidentally rig or drift the two sides. For local testing this means setting the form repository to the **actual git remote** (not a local clone path that resolves elsewhere), matching what was baked.
- **Skills** (`minds-justfile`, `minds-dev-workflow`): document (a) testing the fast path vs ordinary dev iteration, and (b) dev bake (branch/folder, best-effort label) vs production bake (tag, strict content). Make explicit that for a fast-path match the request's canonical repo + branch must equal the baked host's.

## Edge cases and failure modes

- **Local path with no `origin` remote** -> canonicalization raises; `fast_mode=require` errors (decision 6) rather than leasing a mismatched host.
- **URL form differences** (ssh vs https, trailing `.git`/`/`, host case) -> absorbed by normalization; both sides agree.
- **Production bake from a dirty/again-non-tag source** -> rejected; production content must come from a clean tag checkout.
- **Dev bake with uncommitted/unpushed changes** -> allowed; the `repo_branch_or_tag` label identifies the branch but may not byte-match any pushed commit. Documented, not enforced.
- **Missing repo or branch on a fast-path request** -> hard error before the connector call.
- **Slow path** -> unaffected; `relaxed()` still strips repo/branch.
- **Resource mismatch** -> not considered (resources aren't matched yet); a request can adopt a host of any size as long as repo+branch+region match. Acceptable for now (decision 1); revisit when resource matching lands.

## Testing

- **Unit:** the canonicalization function across ssh/https/`.git`/trailing-slash/host-case inputs and local-path-to-origin resolution; the missing-`origin` and missing-identity error paths; the `fast_mode=require` hard-error when repo/branch is absent.
- **Integration / manual:** bake a slice with canonical repo+branch; (a) a create whose canonical repo+branch+region match -> adopts via the fast path; (b) a create with a different branch (or different repo) -> `FastPathUnavailableError` -> slow-path rebuild of the requested spec; (c) `fast_mode=require` with no resolvable repo -> errors.

## Out of scope / future work

- Matching on requested resources (`cpus`/`memory_gb`).
- Promoting `repo_url` to a dedicated `pool_hosts` column.
- Any backwards-compatibility path for pre-existing hosts (they will be re-baked).
- Distinguishing branches from tags in the match (kept as one opaque string).

## Related

- `blueprint/ovh-baremetal-slices/plan-ovh-baremetal-slices.md` -- the slice/pool model these hosts live in.
- `blueprint/slice-fast-path-fixes/` and `blueprint/imbue-cloud-slow-path/` -- adjacent fast/slow-path work.
