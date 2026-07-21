# Private source-of-truth repo with a public open-source mirror

## Purpose and scope

This spec is the decision record and migration plan for moving mngr development into a
private repository while keeping the project open source. The source code of the mngr
product (the published `libs/*` packages and their docs) stays public; ops scripts,
CI/devops internals, infra and telemetry configuration, and internal apps become private.

Audience: the team deciding on and implementing the split. This spec covers the target
architecture, the public/private boundary, the sync machinery, the cutover sequence, and
the guardrails. It does not cover any product or licensing changes beyond repo layout.

Ground facts this plan is built on (verified 2026-07-20):

- `imbue-ai/mngr` is public today: 395 stars, 41 forks, ~21,800 commits, ~1,226 branches,
  232 open PRs, 34 open issues, 147 MB pack.
- Because the repo is public today, all current history is already public. Nothing needs
  retroactive scrubbing; the problem is exclusively the ongoing split going forward.
- 37 `libs/*` packages are published to PyPI via trusted publishing (`publish.yml`,
  environment `pypi`, triggered by `v*` tag pushes).
- No `libs/*` code imports `scripts/`, `apps/`, or `litellm_proxy/` (verified by grep) —
  the boundary is enforceable at the directory level. The only public-to-private edges are
  path-level references to a handful of scripts that will remain public.

## Decision summary

**Hub-and-spoke with a one-way filtered export (the Google/Meta/Dagster pattern):**

1. Create a **new private repo** (name TBD, e.g. `imbue-ai/mngr-private`) seeded with the
   complete existing history, branches, and tags (`git clone --bare` + `git push --mirror`).
   All development, CI, and releases move there.
2. The existing **`imbue-ai/mngr` stays public at its current URL** and becomes a
   read-only mirror of the public subset. It keeps its stars, forks, issues, and every
   existing deep link. It is never flipped private and never renamed.
3. **Copybara** runs the sync: an ITERATIVE private-to-public export (one public commit per
   private commit that touches public paths, with commit-message scrubbing), and a
   CHANGE_REQUEST import that turns community PRs on the mirror into PRs on the private
   repo. Sync state lives in `GitOrigin-RevId:` trailers on public commits; no database.
4. Public `main` is writable **only by the sync bot** (a GitHub App in the ruleset bypass
   list). Community PRs stay open-able; they are imported, land privately, and the
   re-exported commit closes the public PR with credit to the author.
5. Filtering is **allowlist-based**: the copybara config enumerates what leaves the private
   repo. Anything not listed never syncs, including new files added later.

The resulting public repo is, in plain terms: `libs/*` (minus three internal libs), the
handful of scripts that public code and docs depend on, the repo-root essentials, and a
slim public CI — i.e. the open-source mngr product with its full ongoing commit history,
minus everything ops.

Why this direction rather than the inverse (public repo stays source of truth, ops moves
out): see "Alternatives considered" below.

## Alternatives considered

**Flip the existing repo private (+ separate public copy).** Rejected. Making a public
repo private erases stars/watchers and permanently detaches all 41 forks; every public
deep link, badge, and `git clone` breaks. Sourcegraph's 2024 transition documents the
collateral (PR-ref commits inaccessible, links rotted). One-way destruction with no
restore path.

**Rename the public repo and give the private repo the `mngr` name.** Rejected. GitHub
redirects from a rename die the moment the old name is reused, anonymous traffic 404s, and
org members' existing clones would silently re-point at the private repo (their auth
succeeds against the new occupant of the name).

**Inverse overlay (Sentry/VS Code pattern): public repo remains the source of truth for
public code; private ops lives in a separate repo layered on top.** Seriously considered —
it eliminates the entire leak class (private content never enters the public object
database) and needs no sync tooling. Rejected for now because it splits the development
experience: every boundary-crossing change becomes two PRs with no atomic merge (Sentry's
own docs: "commits cannot be merged in lockstep"), and our private set is interleaved with
the tree at the root (`scripts/`, `.github/`, `justfile`, `offload-*.toml`) rather than
being a clean additive layer like VS Code's `product.json` distro. GitLab ran the
two-coupled-repos model for years and abandoned it (a security release spanned ~150 MRs
across the two repos). The filtered mirror keeps the monorepo workflow — one tree, one CI,
one PR — which is what the team and its agents live in. **Note:** if, years from now, the
sync machinery proves burdensome, the overlay is the natural fallback and the
libs-never-import-private invariant (enforced below) keeps that door open.

**Other sync engines.** fbshipit is archived (June 2023; Meta's README points at
copybara). `git filter-repo` run repeatedly is documented-nondeterministic across runs, so
a recurring export would force-push the mirror — disqualified (use it only for one-shot
surgery). `git subtree split`/splitsh-lite are directory-promotion tools with no message
filtering and cannot continue the existing public history. **josh** (used by rust-lang for
`rust-lang/rust` subtree sync, actively maintained) is the credible runner-up: simpler
runtime (single Rust binary), rich path filters, but its commit-message rewriting would
rewrite every commit (breaking continuation of existing public SHAs) and its
PR-import story is weaker. Copybara is the only maintained tool with all of: append-only
continuation of an existing destination history, allowlist path filtering, message
scrubbing, reference re-mapping, PR import, and native tag creation — and it has the most
production precedent for exactly this shape (Google exports, Dagster, Render, thatdot).

## The public/private boundary

The allowlist principle: `origin_files` in the copybara config enumerates the public
subset. Everything else — including any path created in the future — stays private by
default. Adding something to the public subset is a reviewed change to the config.

### Public (synced to the mirror)

| Path | Notes |
|---|---|
| `README.md`, `LICENSE`, `style_guide.md`, `conftest.py`, `.gitignore` | Repo root essentials. Root `conftest.py` imports only public libs (verified). |
| `libs/*` except the private libs below | The product: all 37 published packages, their docs, tests, and per-project `changelog/` dirs. |
| `libs/skitwright` | Unpublished on PyPI but required by `libs/mngr`'s own e2e tests. "Unpublished" is a PyPI concept, not a privacy concept — do not conflate the two lists. |
| `scripts/` public subset | `install.sh` (curled from `raw.githubusercontent.com/imbue-ai/mngr/main/scripts/install.sh` by the README — must stay at this exact path on public `main` forever), `post-source-setup.sh` (RUN by the Dockerfile shipped inside the imbue-mngr wheel), `make_tar_of_repo.sh` (runtime path of published `mngr_schedule`), `open_issue.py` (referenced by CLI error text), `mngr` + `check_mngr_shim.sh` (dev shim), `make_cli_docs.py`, `compile_style_guide.py`, `style_guide.py`, `make_agent_capabilities_doc.py`, `current_branch.sh`, and the changelog-gate modules `check_changelog_entries.py` + `changelog_projects.py` (+ their `_test.py` files; imported by root `test_meta_ratchets.py`). |
| `test_meta_ratchets.py` | Travels with the changelog-gate modules it imports. |
| `justfile` | After the split-file restructuring below. |
| `docs/`-style content inside libs | Already inside `libs/*`; `libs/mngr/future_specs/` is the public design-doc home going forward. |

### Private (never syncs after cutover)

| Path | Notes |
|---|---|
| `apps/*` | `minds` (see open decision — recommended private; FCL-licensed, not MIT), `modal_litellm`, `remote_service_connector`, `slack_exporter`. |
| `libs/mngr_tmr`, `libs/mngr_mapreduce`, `libs/mngr_claude_subagent_proxy` | Internal CI machinery / experiments. `validate_package_graph` guarantees no published wheel depends on them. |
| `scripts/` everything else | Release/ops/agent tooling (`release.py`, `verify_publish.py`, `modal_nuke.py`, cleanup crons, `claude_*.sh`, `qi/`, `josh/`, `lima_image/`, `authorized_github_users.toml`, ...). |
| `.github/**` | All 12 workflows, composite actions, `tmr-authorized-keys`. The mirror gets a fresh slim CI via the overlay (below), never a filtered `ci.yml`. |
| `offload-*.toml`, `offload-history-modal.jsonl`, `.test_durations` | Modal offload configs and CI timing artifacts. |
| `litellm_proxy/`, `.minds/`, `depot.json`, `.mngr/settings.toml`, `test_profiles.toml` | Infra config, Vault ACL policy source, team agent templates. |
| `specs/`, `blueprint/`, `dev/` | Internal design docs and plan-session state. One pre-cutover chore: move `specs/agent-plugin-parity/capability-mixins.md` into `libs/mngr` (the only spec referenced by a public doc, `libs/mngr/docs/concepts/agent_capabilities.md:10`). |
| `private.just` | The ops half of the justfile (below). |
| Root `pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml` | Excluded from `origin_files`; the mirror gets public variants from the overlay (below). |

**Justfile restructuring (pre-cutover, in one commit):** `just` supports optional imports
(`import? 'private.just'` silently skips a missing file). Public recipes (`test-*`,
`build`, `help`, `regenerate-agent-capabilities-doc`, `install-mngr-shim`, `check-changelog`)
stay in `justfile`, which ends with `import? 'private.just'`; all `minds-*`, `tmr-*`,
pool-host, `release`, `deploy`, and `changelog-deploy` recipes move to `private.just`. Both
repos then share a byte-identical `justfile` and the filter simply excludes `private.just`
— no fragile text transforms.

**Sentry:** all six hardcoded DSNs live only in `apps/minds`, which goes private; the
shared sentry code in `libs/imbue_common` and `libs/mngr_latchkey` is fully
parameterized/env-driven and public-safe. No DSN rotation is needed (DSNs are
submission-only by design, ship inside every installed .app regardless, and have been
public for the repo's whole life). One cheap chore: scrub the two `generally-intelligent`
org-slug comments from `libs/imbue_common/imbue/imbue_common/sentry/core.py` (that file
ships in a public wheel).

### Public-only files: the overlay directory

Some files must exist on the mirror but differ from the private tree: a pruned root
`pyproject.toml` (no `apps/*` testpaths/coverage/import-linter roots), a re-locked
`uv.lock`, a reduced `.pre-commit-config.yaml`, and the mirror's own slim CI workflow.
Copybara cannot generate files, and nothing may be hand-pushed to public `main`.

The established resolution (Google exports TensorFlow's public `.github/workflows` from
google3 this way; Meta ships RocksDB's from fbsource): **hand-author the public variants
in the private repo under a shadow path**, e.g. `opensource/overlay/pyproject.toml`,
`opensource/overlay/.github/workflows/ci.yml`, and let the export map them onto the public
root with `core.move("opensource/overlay", "", overwrite=True)`. Humans edit them via
normal private PRs; the bot is still the only writer to public `main`; `destination_files`
stays `glob(["**"])` so copybara owns the whole public tree.

The overlay's `.github/workflows/` set is: the slim public CI, the copybara `pr`-import
workflow, and `copy.bara.sky` itself — the reverse import runs on the mirror via
`pull_request_target`, which is safe with secrets present as long as the workflow treats
PR content strictly as data and never checks out or executes it (that property is a review
requirement on the workflow, and imports are additionally gated on maintainer approval).

The one genuinely derived file is the public `uv.lock`: a private-repo CI job materializes
the public tree (running the same copybara workflow into a `folder.destination`), runs
`uv lock`, and auto-commits the refreshed lock to the overlay when it drifts. The same job
is the **public-buildability gate**: `uv sync --locked`, import-linter, and
`pytest --collect-only` must pass in the materialized public tree before private `main`
changes are considered green. This is the ChromeOS invariant ("public code may never
reference private paths") enforced continuously, and it is what keeps the mirror from
breaking silently.

## Sync machinery

One `copy.bara.sky` in the private repo (synced to the mirror so the reverse workflow can
run there), three workflows, modeled on thatdot/quine's production setup:

- **`push`** (ITERATIVE, private `main` -> public `main`): runs on every push to private
  `main` via GitHub Actions (pinned `copybara_deploy.jar` weekly release on
  `ubuntu-latest`; no bazel, no docker build). One public commit per private commit
  touching `origin_files`; private-only commits produce nothing (no empty commits, no
  leaked messages). `check_last_rev_state = True` so destination drift fails loudly
  instead of being silently overwritten. An Actions `concurrency:` group serializes runs.
- **`pr`** (CHANGE_REQUEST, public PR -> private PR): triggered by `pull_request_target`
  on the mirror (gated: `review_state`/`required_labels` so only maintainer-approved PRs
  import; the workflow never executes PR code). `metadata.save_author` records the
  community author; the eventual export restores them as git author
  (`metadata.restore_author(search_all_changes=True)`) and appends
  `Closes imbue-ai/mngr#N`, which auto-closes the public PR when the commit lands on
  public `main` — loop-free by construction (the pr workflow only reads OPEN PRs).
- **`initialize`** (SQUASH): manual repair tool only.

Commit-message policy (transformations on `push`):

- `metadata.map_references` rewrites bare `#N` / `!N` references to fully-qualified
  private references — an unrewritten `#N` would be reinterpreted against *public*
  numbering and can close an unrelated public issue.
- `metadata.scrubber` strips a designated `INTERNAL:` block convention and internal ticket
  patterns. Start with this opt-out style (matches current commit culture, and all
  history is already public anyway); revisit opt-in (`<public>` tags with
  `fail_if_no_match`) if genuinely sensitive private work starts.
- `core.replace` strips `BEGIN-INTERNAL`/`END-INTERNAL` blocks from file contents,
  giving a sanctioned way to keep private notes in public files.
- Issue closure is deliberate: writing `Fixes #34` in a private commit does nothing at
  private-merge time; when the message syncs, GitHub evaluates it in the public repo's
  context and closes public issue 34, attributed to the public SHA. That is the intended
  mechanism for community-facing fixes.

Bootstrap: the first `push` run uses `--last-rev <fork-point-sha> --force` (the public
repo has no `GitOrigin-RevId` labels yet). Never use `--init-history` against the existing
public repo — it replays the entire history into the destination.

Auth: a dedicated GitHub App ("mngr-sync"); its installation token is the only ruleset
bypass on public `main`. No PATs on human accounts; humans (including admins) stay out of
the bypass list.

## Releases and tags

- The full publish pipeline moves to the private repo: `publish.yml`,
  `publish-tombstones.yml`, `release-tests.yml`, the `pypi` environment, and the `v*`
  tag-push trigger. `scripts/release.py` works unchanged (it operates on `origin` = the
  private repo, which has the complete historical tag set from the mirror seed).
- **Re-register all 37 PyPI trusted publishers** against the private repo (owner + repo +
  `publish.yml` + environment `pypi`). Repo visibility is irrelevant to trusted
  publishing. There is no PyPI management API for this: ~37 manual web-UI edits, done
  add-new-before-remove-old so there is never a gap, and the public repo's publisher
  entries are removed at the end so a compromised mirror can never publish.
- Public `v*` tags: `release.py` adds a `RELEASE_TAG=v<version>` trailer to the release
  commit; the copybara destination sets `tag_name = "${RELEASE_TAG}"` (+`tag_msg`), so the
  migrated release commit is tagged on the mirror automatically. Public tags matter: the
  shipped CLI's help deep-links (`doc_links.py`) are pinned to release blob URLs, and
  outsiders need "which public commit is v0.2.18".
- `minds-v*` tags point at older SHAs (`GREEN_MNGR_SHA`), which per-change `tag_name`
  cannot express. A small private-repo workflow on `push: tags:` maps private SHA ->
  public SHA via the `GitOrigin-RevId` trailer (walking first-parent ancestors when the
  tagged commit produced no public commit), then pushes the annotated tag to the mirror
  with the App credential, with retry for sync lag and force-repoint support. This is the
  kubernetes/publishing-bot pattern. (Only needed if minds tags remain public — see open
  decisions; `install-wsl.sh` currently clones the public repo at the latest `minds-v*`.)
- The `qemu-img-v10.2.2` GitHub Release (and future public build-asset releases) stays on
  the public repo so unauthenticated build-time downloads keep working.

## CI after the split

**Private repo:** all 12 existing workflows run as-is, but every repo-bound integration
must be re-bound (none of these travel with a git push):

- Vault: add the private repo to the `mngr_ci_gh` / `minds_ci_env_gh` / `minds_ci_test_gh`
  JWT role bound-claims in the `imbue-ai/vault` Terraform. Until this lands, every
  secrets-fetching job fails.
- GitHub environments `pypi`, `minds-ci-env`, `minds-ci-test`; repo var
  `DISABLE_MINDS_SNAPSHOT_CI`; branch protections; merge settings; labels.
- Self-hosted Mac runners: register to the private repo (org runner group), and
  **deregister from the public repo** — self-hosted runners on a public repo are an
  explicit GitHub security anti-pattern, so the move is a net security win.
- Modal offload is visibility-independent (checkout + Vault-sourced tokens; no anonymous
  clone anywhere in the path) — verified, no changes needed.
- Actions billing: self-hosted runners and Modal are free/external; only GitHub-hosted
  Linux orchestrator minutes start metering (Team plan: 3,000 min/mo included). Verify the
  org's plan and expected minutes before cutover; keep macOS jobs on self-hosted (hosted
  macOS is 10x the Linux rate).

**Public mirror:** a single slim workflow (from the overlay): lint + the Vault-free
unit/integration subset on GitHub-hosted runners, providing signal on community PRs. No
secrets, no self-hosted labels, nothing reachable from fork PRs. Secret scanning + push
protection enabled (free on public repos) as defense-in-depth on the bot's pushes.

## Cutover plan

**Phase 0 — prep (days before, no freeze):**

1. Decide the open decisions below; land the pre-cutover chores (justfile split, spec
   move, org-slug comment scrub, stray `changelog/mngr-tmr-qi.md`, vestigial `main.py`).
2. Create the private repo; seed with `git clone --bare` + `git push --mirror` (a `--bare`
   clone, not `--mirror`, to avoid GitHub's read-only `refs/pull/*` rejections).
3. Re-bind integrations: Vault Terraform roles, runners, environments, PyPI publishers
   (add-new), bot `GH_TOKEN` access.
4. Author the copybara config + overlay files + slim public CI in the private repo. Unit
   test the filter: a test that materializes the workflow against a synthetic tree and
   asserts the exact expected file set (the fbshipit discipline).
5. **Dry-run everything**: full CI green on a scratch branch in the private repo (Vault ->
   offload -> Mac runners -> a manual TMR dispatch), a copybara `--dry-run` export against
   a fork of the mirror, and a burst benchmark (replay ~50 commits) to validate ITERATIVE
   throughput at our commit rate.

**Phase 1 — freeze and flip (short window, ~half a day):**

6. Merge freeze; authors push WIP branches (branches carry over; PR discussion does not).
   Land what is ready.
7. Server-side freeze on the public repo: enable the `main` ruleset (bot-only bypass),
   disable Actions. Because the public repo keeps existing at the old URL, there are no
   redirects — stale remotes must *fail loudly*, not silently keep pushing internal work
   to the public repo. This ruleset is the only mechanism that guarantees that.
8. Final `git push --mirror` to the private repo; verify branch/tag counts.
9. Land the **hand-authored cutover commit on public `main`** (as the bot, human-reviewed
   in the private repo first): delete all private paths, add the overlay-derived public
   variants (pruned `pyproject.toml`, re-locked `uv.lock`, slim CI, reduced pre-commit).
   Do not leave private paths frozen in place: frozen scheduled workflows keep executing
   from the default branch indefinitely, and a frozen `uv.lock` breaks against evolving
   `libs/*` anyway.
10. First copybara run with `--last-rev <fork-point> --force`. **It must be a near-noop**
    — that is the acceptance test that `origin_files`/transformations exactly match the
    cutover commit. Then enable the on-push trigger.

**Phase 2 — recreate in-flight work:**

11. Open PRs cannot be transferred (GitHub moves issues only). Script it with `gh`: for
    each recently-active internal PR, recreate against the private repo from the same
    (already-mirrored) branch with original title/body + a link to the public PR, then
    comment-and-close the public original. Stale PRs: close with a pointer. The 7 fork
    PRs stay on the mirror as community PRs.
12. Issues: community-relevant issues stay on the public repo permanently (it remains the
    `mngr report-issue` target). Transfer internal-only issues private (public->private
    transfer works and redirects; the reverse is impossible — so default to leaving
    anything community-relevant public).
13. Accept that `#N` numbering resets in the private repo; `metadata.map_references`
    handles old references in synced messages.

**Phase 3 — re-point every bot and human:**

14. Redeploy the changelog-consolidation Modal schedule from a re-pointed checkout
    (`scripts/changelog_deploy.sh`) — deployed schedules bake the origin URL and a
    `GH_TOKEN` into the image and would otherwise silently keep fetching stale public
    `main` and opening PRs against the mirror.
15. Rewrite `~/.mngr/release-candidate/RUNBOOK.md` + `state.json` (they pin
    `imbue-ai/mngr`, PR #2484, and public `refs/pull/N` numbers); recreate the RC draft PR
    privately; restart the loop.
16. First private commit: update the four `git remote set-url origin ...mngr.git` lines in
    `.mngr/settings.toml`, `scripts/post-source-setup.sh:63`'s fallback,
    `scripts/release.py:70` `ACTIONS_URL`, `scripts/release_tombstones.py:159`, and the
    Slack commit-link in `minds-launch-to-msg.yml`. Deliberately keep pointing at the
    PUBLIC repo: `scripts/install.sh` consumers, `doc_links.py`, `issue_reporting.py`
    (`GITHUB_REPO`), and lib `pyproject.toml` Homepage/Repository URLs.
17. Developers re-point clones (`git remote set-url origin`); existing mngr agents need
    recreate or `--reuse --update`.

## Guardrails (steady state)

- **Allowlist filtering** — new paths are private until explicitly added to the config.
- **Filter unit test** in the private repo (exact expected file set for a synthetic tree).
- **Public-buildability gate** in private CI (materialized public tree must lock, sync,
  import-lint, and collect tests).
- **`check_last_rev_state = True`** + bot-only ruleset — public drift fails the sync
  loudly and nobody can push around the bot.
- **Secret scanning + push protection** on the mirror (free); optionally GitHub Secret
  Protection on the private repo ($19/committer/mo) so secrets are caught before the sync
  pipeline ever sees them. Both are defense-in-depth only; the allowlist is the real wall.
- **Alerting on sync failure** (the export workflow pages/Slacks on red) — a silently
  stale mirror is how filtered mirrors decay (GitLab's gitlab-foss).
- **A named owner** for the sync machinery (config, copybara version pin, repair runbook:
  `--last-rev`/`--force`, the `initialize` workflow). Unowned mirrors rot.
- **Published artifacts are part of the boundary**: wheels, the minds .app, and source
  maps leak independently of the repo (the minds .app already bundles 16 workspace
  packages' pure-Python source; Anthropic's Claude Code source escaped via an npm source
  map, not a repo). Nothing in a shipped artifact should be considered private.

## Open decisions (need human input before Phase 0)

1. **`apps/minds`: private or public?** Recommended private (it is FCL-1.0-MIT
   fair-source, not MIT; not on PyPI; its ops coupling is total). But FCL exists to share
   commercial code publicly — if the license was a deliberate fair-source strategy rather
   than a defensive one, keep it public, or take the middle path: private live
   development + per-release tagged source export. This decision also determines whether
   the `minds-v*` tag re-pointer and public `install-wsl.sh` path survive.
2. **Private repo name** (`mngr-private`? `mono`?). Never reuse `mngr`.
3. **Public mirror CI scope**: slim lint+unit workflow (recommended) vs none at all.
4. **`CLAUDE.md` / `.claude/` / `.agents/`**: public (useful to OSS contributors using
   agents; describes internal infra workflow) or private with a public variant in the
   overlay. Leaning: keep a trimmed public variant.
5. **GitHub plan check**: confirm org plan (Team vs Enterprise) and projected
   GitHub-hosted Linux minutes for orchestrator jobs.
6. **Commit-message policy**: opt-out scrubbing (recommended initially) vs wholesale
   message replacement vs opt-in `<public>` tags.

## Effort estimate

- **Phase 0 (prep)**: the bulk of the work — copybara config + overlay + filter tests +
  slim CI + integration re-binding + dry runs. Roughly one to two weeks of part-time
  effort for one person, most of it iterating on the filter config with `--dry-run`.
- **Phase 1 (freeze and flip)**: about half a day, scriptable and rehearsed by the Phase 0
  dry run.
- **Phases 2-3 (recreate + re-point)**: a day of scripted `gh` work plus a few days of
  stragglers (agents, deployed schedules) surfacing as loud failures.
- **Steady state**: near-zero marginal cost per commit; the real ongoing cost is the named
  owner keeping the filter config, copybara pin, and runbook healthy, plus the occasional
  community-PR import.

## Risks

- **Copybara throughput** at our commit rate (ITERATIVE is one-commit-at-a-time; HN
  reports call large exports slow). Mitigated by the Phase 0 burst benchmark; fallback is
  batching (scheduled rather than per-push runs) — state is in the destination, so
  batching is safe.
- **Mixed commits** (one commit touching public + private paths) export their full message
  and the public part of their diff. The scrubber and team convention (`INTERNAL:` blocks)
  are the mitigation; the failure mode is embarrassment, not secret leakage, since the
  allowlist bounds file content.
- **Maintenance bet on copybara**: weekly "test release" jars, community actions churn.
  Pin a version, own the upgrade cadence; josh is the documented fallback engine.
- **Contributor friction**: community PRs get imported rather than merged directly, adding
  latency and an unfamiliar flow. Mitigation: CONTRIBUTING.md explains the flow (the
  abseil/React precedent shows communities accept it); fake-merge integration
  (`COPYBARA_INTEGRATE_REVIEW`) preserves merged-PR credit.
- **The window where a stale bot leaks**: any deployed automation still holding the old
  origin (Modal schedules, agents) pushes to the public repo post-cutover. The ruleset
  makes these fail loudly; Phase 3 exists to chase them down.

## References

- Copybara: repo `google/copybara`; reference docs `docs/reference.md`; production
  configs: `thatdot/quine` (`.github/workflows/copy.bara.sky` — the closest analog),
  `render-oss/cli`, `wirequery/wirequery`.
- Dagster, "Monorepos, the hub-and-spoke model, and Copybara" (April 2026) — provenance
  labels, merge-queue sync gate, loop prevention.
- GitLab, "A single codebase for GitLab Community and Enterprise Edition" — the
  two-coupled-repos failure mode.
- Sentry `develop.sentry.dev/sentry-vs-getsentry` and VS Code
  `wiki/Differences-between-the-repository-and-Visual-Studio-Code` — the inverse overlay
  pattern.
- kubernetes/publishing-bot — tag re-pointing via provenance trailers.
- Sourcegraph postmortems (2023 token leak; 2024 going-dark collateral) — why the public
  repo must stay alive and why scanning is not the wall.
- GitHub docs: repository visibility changes, renaming, rulesets, Actions billing,
  issue transfer; PyPI trusted publishers docs.
