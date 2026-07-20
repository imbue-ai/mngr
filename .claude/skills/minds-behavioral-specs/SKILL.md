---
name: minds-behavioral-specs
description: The definitional reference for how minds expresses behavioral specifications - Gherkin .feature files under apps/minds/specs/, their folder and tag organization, coordinates, invariants as Rule blocks with folder scoping, overview/sidecar prose files, the witnesses test back-link convention, and the minds specs CLI. Use whenever reading, writing, validating, querying, or otherwise reasoning about behavioral specs or .feature files in this repo.
---

# minds behavioral specs

This skill defines the behavioral-spec language used by minds: what the
artifacts are, where they live, and what their syntax and structure mean.
Processes that create, update, or consume specs are out of scope here.

## What a behavioral spec is

A behavioral spec describes the externally observable behavior of a minds
surface: the flows a user or client can take (scenarios) and the properties
that hold across all flows and states (invariants).

The language's register:

- Specs describe observable behavior only. How a test drives the system
  (test clients, waits, selectors, fixtures) never appears.
- Protocol details (paths, status codes, redirect targets) appear only where
  they are part of the observable contract of the surface being specified.
- Spec files never reference tests. The link runs the other way (see
  "Tests back-link to specs").

Behavioral specs are a distinct artifact class. Do not confuse them with the
repo-root `specs/` and `blueprint/` directories (design documents and
implementation plans) or with `docs/` (user-facing documentation).

## Where specs live

The corpus root is `apps/minds/specs/`. Folders group specs by area; a folder
holds `.feature` files (one Feature each) plus optional prose files:

```
apps/minds/specs/
  overview.md                # context for the whole corpus
  authentication/
    overview.md              # context for authentication/ and below
    invariants.feature       # Rules that hold for authentication/ and below
    signin.feature           # one Feature: the sign-in flow
    signin.md                # optional prose sidecar for signin.feature
    session.feature
```

Naming and structure rules:

- Folder names and file basenames are kebab-case.
- Two filenames are reserved in every folder: `invariants.feature` (see
  "Invariants and scope") and `overview.md` (prose context for the folder and
  everything below it). No `.feature` file may be named `overview`, since that
  would make `overview.md` read as its sidecar.
- Any other `.md` file is the sidecar of the `.feature` file with the same
  basename in the same folder; `invariants.md` is simply the sidecar of
  `invariants.feature`. An `.md` file with no matching `.feature` is invalid.
- Every folder has the same semantics, including the corpus root itself.
  Nesting is permitted; depth carries no special meaning.

Relationships between files are positional - expressed by basename and folder,
never by links or paths written inside `.feature` files.

## Syntax: what "valid" means

A `.feature` file is syntactically valid if and only if `gherkin-official`
(the Cucumber reference parser, classic `.feature` syntax) parses it; the
version pinned by `apps/minds` is the arbiter. The language uses the default
English keywords only - no `# language:` headers. The available constructs
are `Feature`, `Background`, `Scenario` (synonym `Example`),
`Scenario Outline` with `Examples` tables, `Rule`, the step keywords
`Given` / `When` / `Then` / `And` / `But`, data tables, doc strings, and `#`
comments.

A representative file:

```gherkin
Feature: Sign-in with a one-time login code
  The desktop client prints a login URL to its terminal at startup.
  Opening that URL is the only way to establish a session in a browser
  that has none.

  Background:
    Given a running desktop client

  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    Given the user is not signed in
    When the user opens the login URL in a browser
    Then the browser lands on the home page "/"
    And the user is signed in

  @missing-code
  Scenario Outline: Requests without a code are malformed input
    When a request is made to "<path>" with no one-time code parameter
    Then it is rejected as malformed input

    Examples:
      | path          |
      | /login        |
      | /authenticate |
```

Structural content - declarations, steps, tables - is normative. Description
slots (the free prose under `Feature:`, `Rule:`, `Scenario:`, and
`Scenario Outline:` headers, before the first step or child) and `.md` files
explain, but do not define. The language does not partition explanatory prose
between description slots and prose files; that split is the author's choice.

## Identity: tags and coordinates

Tags may appear wherever Gherkin permits them: on units - `Scenario`,
`Scenario Outline`, and `Rule` - and on `Feature` and `Examples` blocks.
Every unit carries at least one tag.

- The first tag on a unit is its identity. A `Scenario Outline` has one
  identity covering all of its Examples rows.
- Tags after the first are auxiliary labels; they may repeat across units
  and have no defined semantics.
- Tags on `Feature` and `Examples` blocks have no defined semantics either -
  in particular, they do not cascade to scenarios - but each claims a
  coordinate (below) and must be unique like one.
- All tags are short kebab-case names that do not encode anything their
  location already says.

A unit's coordinate - the stable handle everything outside the spec uses to
refer to it - joins the folder names on the path from the corpus root to the
unit's file, then its raw identity tag, with dots:

- `@fresh-code` in `apps/minds/specs/authentication/signin.feature` has the
  coordinate `authentication.fresh-code`.
- `@no-tls` in `apps/minds/specs/networking/tunnels/hole-punching.feature`
  has the coordinate `networking.tunnels.no-tls`.
- `@single-use-codes` in `apps/minds/specs/invariants.feature` has the
  coordinate `single-use-codes` - zero folders on the path, so the
  coordinate is the raw tag alone.

Counter-example - the common wrong guess: the coordinate of `@fresh-code`
above is NOT `authentication.signin.fresh-code`. File basenames never appear
in coordinates; only folders qualify. This is what lets a scenario move
between files in its folder - or a file be renamed or split - without any
unit changing identity.

A coordinate is claimed by each unit's identity tag and by each tag on a
`Feature` or `Examples` block; auxiliary tags claim nothing. No coordinate
is claimed twice - equivalently, all claiming tags are unique within their
folder. The `@` sigil is Gherkin tag syntax and stays in the file;
coordinates are bare dotted names.

## Invariants and scope

An invariant is a property that must hold across all scenarios, states, and
interleavings within its scope - not just the flows spelled out in scenarios.
Invariants are written as `Rule:` blocks:

- The Rule name states the property; the Rule description carries the
  rationale.
- Identity works exactly as for scenarios: the first tag.
- A Rule may stand alone, or carry illustrating scenarios as children
  (`Scenario Outline` children included). Each child is a unit with its own
  identity tag.

Nothing in a tag marks a unit as an invariant. The kind is structural: being
a `Rule:` is what makes it an invariant, and tooling reports the unit kind.

Scope is determined entirely by which file the Rule lives in:

- A `Rule:` in an ordinary feature file applies to that file's Feature.
- A `Rule:` in a folder's `invariants.feature` applies to that folder and
  everything below it.
- This holds at every level: in `apps/minds/specs/authentication/invariants.feature`
  a Rule binds all of `authentication/`; in `apps/minds/specs/invariants.feature`
  it binds the entire corpus.

A file-scoped Rule in an ordinary feature file:

```gherkin
Feature: Session lifetime

  @survives-restart
  Scenario: Sessions survive a desktop-client restart
    Given a signed-in user
    When the desktop client is stopped and started again
    Then the user is still signed in

  @installation-bound-tokens
  Rule: Only session tokens minted by this installation are accepted
    A token created under another data directory is treated as signed out.
```

Gherkin nests every scenario that follows a `Rule:` header under that Rule,
so file-scoped Rules come after the file's ordinary scenarios.

A subtree-scoped Rule in an `invariants.feature`, with an illustrating child:

```gherkin
Feature: Authentication invariants

  @single-use-codes
  Rule: A one-time code grants at most one session, ever
    Every presentation of an already-spent code is refused, under any
    interleaving of requests. Rationale: the login URL is written in plain
    text to a terminal; single use bounds that exposure.

    @spent-code-refused
    Example: A spent code cannot sign anyone in again
      Given the login URL has already been used to sign in
      When anyone presents the same code again
      Then authentication is refused
```

## Tests back-link to specs

A test that verifies a spec unit declares it, using the unit's coordinate:

```python
@pytest.mark.witnesses("authentication.fresh-code")
def test_authenticate_with_valid_code_sets_cookie() -> None: ...

@pytest.mark.witnesses("authentication.prefetch", partial="does not assert the code remains unspent")
def test_login_page_redirects_via_script() -> None: ...
```

- `partial=` states what the test does not cover; omit it when the test
  covers the unit fully.
- A test may carry several `witnesses` markers.
- The marker is registered in the shared pytest settings and usable from any
  project in the monorepo.

## Tooling

`minds specs` is the CLI over the corpus (run as `uv run minds specs ...`
from the repo root):

- `validate` - parses every spec file and enforces the rules in this
  document.
- `list` - emits the corpus as JSONL, one record per unit (scenario,
  scenario outline, or rule), carrying coordinate, unit kind, and location.
- `query` - the same records, filtered structurally (by tag, name, or step
  text).

`uv run minds specs --help` is authoritative for invocation detail. For
AST-level needs beyond the CLI, `gherkin-official` (a dependency of
`apps/minds`) is importable directly.
