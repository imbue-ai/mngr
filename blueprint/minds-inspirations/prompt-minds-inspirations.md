# Inspirations — feature prompt

Inspirations let a running mind **publish** a clean, shareable snapshot of the apps/features it built, and let another mind **use** (adapt) an existing inspiration.

- Everything ships in the **forever-claude-template (FCT)** — there are **no `apps/minds` changes** and **no `system_interface` changes**: all interaction happens inline in chat (**no popups anywhere**; the earlier publish popup and GitHub-login modal were removed).
- `CLAUDE.md` gets **one sentence** noting that inspirations exist. The agent does **not** proactively offer to publish — publishing is user-initiated for now.
- Out of scope: a catalog/discovery UI for browsing inspirations (later effort).

## `/publish-inspiration`

- Ask the user what to capture: a name, and which apps/features to include (don't enumerate files). **No user data by default** — include it only on explicit request.
- Delegate the repo assembly to a **`launch-task` worker on its own isolated worktree** — one worker cycle — so the live mind stays untouched. **Never merge back**: nothing merges into or writes to the live mind's checkout after assembly starts.
- The worker runs `build_inspiration.sh`: **reset to the FCT version the mind was based on** (clean base — no upstream fetch; the reset must drop tracked-but-not-in-base files), overlay only the selected paths, strip secrets, and make one commit; write an inspiration-specific `/welcome` skill into the snapshot (the template's own welcome is untouched); run a boot smoke-check (must boot; selected apps may have holes).
- The worker then fleshes out the `inspiration-<name>.md` manifest's FILL-IN sections (front-matter: `title`/`description`/`thumbnail`; body: what it is, how it works, how to adapt it, holes, permissions it may need, adaptation history) and designs a **bespoke, app-relevant `inspiration-<name>.svg` thumbnail** (mock data only, never real user data). A deterministic placeholder-marker gate blocks publishing the generic template SVG.
- Confirm **inline in chat** (no popup): the agent presents the editable title, description, repo name, private/public choice, and thumbnail. If `gh auth status` fails, surface the **`gh` device flow in chat** (one-time code + the github.com/login/device link), requesting the `workflow` scope, with `GH_TOKEN`/`GITHUB_TOKEN` scrubbed so the credential persists to gh's store, then `gh auth setup-git` — **no agent restart**.
- Create a **new** GitHub repo (private by default) and push **directly from the worker's worktree**. Record a **provenance link** to the FCT version only — no fetching, pulling, or updating from upstream.
- A single repo can **accumulate multiple inspirations** over time (one manifest per inspiration at the root; each may contain multiple apps).

## `/use-inspiration`

- Two entry points: (1) a **new mind created from an inspiration repo** as its template, where the snapshot's generated `/welcome` drives adaptation on startup; (2) **`/use-inspiration <git-url>`** merges an inspiration into the current mind.
- Read the relevant `inspiration-<name>.md`, ask the user how they want to adapt it, and **fill the holes interactively** in plain language (e.g. swapping Slack for email).
- The manifest doubles as a **worksheet**: append a dated "how it was adapted" record. Merged-in manifests accumulate at the root.
