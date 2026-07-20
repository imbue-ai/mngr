---
name: minds-behavioral-specs
description: The definitional reference for how minds expresses behavioral specifications - Gherkin .feature files under apps/minds/specs/, their folder and tag organization, coordinates, invariants as Rule blocks with folder scoping, overview/sidecar prose files, the witnesses test back-link convention, and the minds specs CLI. Use whenever reading, writing, validating, querying, or otherwise reasoning about behavioral specs or .feature files in this repo.
---

# minds behavioral specs

This skill defines the behavioral-spec language used by minds: what the
artifacts are, where they live, and what their syntax and structure mean. It
prescribes no workflow. Processes that create, update, or consume specs are
defined elsewhere and refer back to this document.

## What a behavioral spec is

A behavioral spec describes the externally observable behavior of a minds
surface: the flows a user or client can take (scenarios) and the properties
that hold across all flows and states (invariants).

The language has a deliberate register:

- Specs describe observable behavior only. How a test drives the system
  (test clients, waits, selectors, fixtures) never appears.
- Protocol details (paths, status codes, redirect targets) appear only where
  they are part of the observable contract of the surface being specified.
- Spec files never reference tests. The link runs the other way (see
  "Tests back-link to specs" below).

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
- Two basenames are reserved in every folder: `invariants.feature` (see
  "Invariants and scope") and `overview.md` (prose context for the folder and
  everything below it). These names must not be shadowed: no `.feature` file
  named `overview`, no `.md` file named `invariants`.
- Any other `.md` file is the sidecar of the `.feature` file with the same
  basename in the same folder.
- Every folder has the same semantics, including the corpus root itself.
  Nesting is permitted; depth carries no special meaning.

Relationships between files are positional - expressed by basename and folder,
never by links or paths written inside `.feature` files.

## Syntax: what "valid" means

A `.feature` file is syntactically valid if and only if `gherkin-official`
(the Cucumber reference parser, classic `.feature` syntax, English keywords)
parses it. The version pinned by `apps/minds` is the arbiter. This skill does
not restate the Gherkin grammar; the parser is the authority.

On top of parseability, the language imposes these rules (enforced by
`minds specs validate`):

- Kebab-case folder names, file basenames, and tags.
- Every `Scenario`, `Scenario Outline`, and `Rule` carries at least one tag;
  the first tag is its identity (see next section).
- Identity tags are unique within their folder subtree.
- Reserved basenames are not shadowed.

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

Free prose belongs in the description slots (under `Feature:`, `Rule:`,
`Scenario:` headers, before the first step) and in the prose files
(`overview.md`, sidecars). Prose explains; only Feature-file content defines
behavior.

## Identity: tags and coordinates

The first tag on a `Scenario`, `Scenario Outline`, or `Rule` is its identity.

- Raw tags are short kebab-case names with no prefixes. Do not encode in the
  tag anything its location already says.
- A `Scenario Outline` has one identity covering all of its Examples rows.
- Tags after the first are auxiliary labels: kebab-case, exempt from
  uniqueness, and currently carrying no defined semantics.

A unit's coordinate - the stable handle everything outside the spec uses to
refer to it - is derived from its location: the folder path relative to the
corpus root, with `/` replaced by `.`, then `.`, then the raw tag.

- `@fresh-code` in `apps/minds/specs/authentication/signin.feature` has the
  coordinate `authentication.fresh-code`.
- `@no-tls` in `apps/minds/specs/networking/tunnels/hole-punching.feature`
  has the coordinate `networking.tunnels.no-tls`.

Counter-example, because this is the common wrong guess: the coordinate of
`@fresh-code` above is NOT `authentication.signin.fresh-code`. File basenames
never appear in coordinates. Only folders qualify. This is what lets a
scenario move between files in its folder - or a file be renamed or split -
without changing any unit's identity.

The `@` sigil is Gherkin tag syntax and stays in the file; coordinates are
bare dotted names.

## Invariants and scope

An invariant is a property that must hold across all scenarios, states, and
interleavings within its scope - not just the flows spelled out in scenarios.
Invariants are written as `Rule:` blocks:

- The Rule name states the property.
- The Rule description carries the rationale.
- Like scenarios, a Rule's first tag is its identity.
- A Rule may have no children, or illustrating `Example:` scenarios as
  children (each with its own identity tag).

Nothing in a tag marks a unit as an invariant. The kind is structural: being
a `Rule:` is what makes it an invariant, and tooling reports the unit kind.

Scope is determined entirely by which file the Rule lives in:

- A `Rule:` in an ordinary feature file applies to that file's Feature.
- A `Rule:` in a folder's `invariants.feature` applies to that folder and
  everything below it.
- This holds at every level: in `apps/minds/specs/authentication/invariants.feature`
  a Rule binds all of `authentication/`; in `apps/minds/specs/invariants.feature`
  it binds the entire corpus.

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
- The direction is one-way: tests name coordinates; spec files never name
  tests. The marker is registered in the shared pytest settings and usable
  from any project in the monorepo.

## Tooling

`minds specs` is the CLI over the corpus (run as `uv run minds specs ...`
from the repo root):

- `validate` - parses every spec file and enforces the language rules above.
- `list` - emits the corpus as JSONL, one record per unit (scenario,
  scenario outline, or rule), carrying coordinate, unit kind, and location.
- `query` - the same records, filtered structurally (by tag, name, or step
  text).

`uv run minds specs --help` is authoritative for invocation detail. For
AST-level needs beyond the CLI, `gherkin-official` (already a dependency of
`apps/minds`) is importable directly.
