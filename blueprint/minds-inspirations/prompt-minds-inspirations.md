# Inspirations — feature prompt

Inspirations let a running mind **publish** a clean, shareable snapshot of the apps/features it built, and let another mind **use** (adapt) an existing inspiration.

- Everything ships in the **forever-claude-template (FCT)** — there are **no `apps/minds` changes** (the minds desktop client proxies the in-container `system_interface` web UI generically, so new routes/modals appear without minds-side awareness).
- `CLAUDE.md` gets **one sentence** noting that inspirations exist. The agent does **not** proactively offer to publish — publishing is user-initiated for now.
- Out of scope: a catalog/discovery UI for browsing inspirations (later effort).

## `/publish-inspiration`

- Ask the user what to capture: a name, and which apps/features to include (don't enumerate files). **No user data by default** — include it only on explicit request.
- Delegate the repo assembly to a **`launch-task` sub-agent on its own worktree**, so the live mind stays untouched.
- In the worktree: **reset to the FCT version the mind was based on** (clean base — no upstream fetch; the reset must drop tracked-but-not-in-base files), overlay only the selected paths, strip secrets, and make one commit.
- Generate an `inspiration-<name>.md` manifest (front-matter: `title`/`description`/`thumbnail`; body: what it is, apps included, holes, permissions it may need, adaptation history) and an `inspiration-<name>.svg` thumbnail; rewrite the `/welcome` stable region for the latest inspiration; run a boot smoke-check (must boot; selected apps may have holes).
- Confirm via a **`system_interface` popup** — a small box with editable title, description, repo name, private/public toggle, and thumbnail preview. If `gh auth status` fails, a **`system_interface` GitHub-login modal** logs the user in (configures gh's store + git credential helper; **no agent restart**).
- Create a **new** GitHub repo (private by default) and push. Record a **provenance link** to the FCT version only — no fetching, pulling, or updating from upstream.
- A single repo can **accumulate multiple inspirations** over time (one manifest per inspiration at the root; each may contain multiple apps).

## `/use-inspiration`

- Two entry points: (1) a **new mind created from an inspiration repo** as its template, where the rewritten `/welcome` drives adaptation on startup; (2) **`/use-inspiration <git-url>`** merges an inspiration into the current mind.
- Read the relevant `inspiration-<name>.md`, ask the user how they want to adapt it, and **fill the holes interactively** in plain language (e.g. swapping Slack for email).
- The manifest doubles as a **worksheet**: append a dated "how it was adapted" record. Merged-in manifests accumulate at the root.
