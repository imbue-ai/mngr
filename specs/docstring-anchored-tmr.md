# Docstring-anchored TMR

## Motivation

TMR (test map-reduce, `libs/mngr_tmr`) fans out one agent per test; each agent
runs its test and, if needed, fixes the test or the implementation, keeping the
test at its intended scope. The scope anchor used to be the *tutorial block* the
test corresponds to. That made TMR specific to the mngr e2e tutorial tests: the
mapper/reducer prompts talked about `mega_tutorial.sh`, `e2e.write_tutorial_block`,
keeping tests "1:1 with the tutorial", and the "natural size" of a tutorial
block. None of that applies to the non-tutorial release tests (install, upgrade,
docker-provider, cli) or the per-provider release tests in `mngr_aws`, `mngr_gcp`,
`mngr_azure`, `mngr_modal`, etc.

Future TMR runs target **all** mngr release tests, so the anchor must be
something every test has: its **docstring**.

## The scheme

Each test's docstring is the contract for its scope. The TMR agent makes the
test verify exactly what the docstring describes -- adding missing coverage,
removing gold-plating -- and **never edits the docstring** (one exception below).

Right-sizing thus moves *out* of TMR runtime (where the mapper re-derived the
"natural size" every run) and *into* authoring time: the
`sync-tutorial-to-e2e-tests` skill and the one-time migration crystallize the
scope into the docstring once; TMR thereafter only enforces conformance to it.

### Docstring format

**Tutorial-anchored tests** (those under `libs/mngr/imbue/mngr/e2e/tutorial/`,
each corresponding to a block in `mega_tutorial.sh`): the docstring is strictly
the verbatim tutorial block followed by the discretionary scope.

```python
def test_help_succeeds(e2e: E2eSession) -> None:
    """Tutorial block:
        # or see the other commands--list, destroy, message, connect, and more!
        mngr --help

    Scope: the command exits 0 and its output names the documented
    subcommands (create, list, destroy, message, connect, clone). Asserting on
    those names is the effect; do not pin incidental help wording.
    """
```

- The `Tutorial block:` section holds the block **verbatim** (indented under the
  header). It is the lower bound on scope and is kept in sync with
  `mega_tutorial.sh` by `scripts/tutorial_matcher.py`.
- The `Scope:` prose crystallizes the implicit requirements of the command(s):
  the real effect to observe, the flag-level difference, etc. -- the same
  "golden rule" reasoning that used to live in the mapper prompt, but resolved
  once, per test, by a human/skill rather than re-derived every TMR run.
- No leading summary line: the docstring is *only* the block plus the scope.

**Non-tutorial tests** (everything else): no `Tutorial block:` section; the
docstring is the scope prose. There is no explicit lower bound.

### The one docstring-mutation exception: `FIX_TUTORIAL`

The docstring is immutable to the TMR agent except for one case: if a test has a
`Tutorial block:` section and that block is genuinely outdated relative to the
command's actual behavior, the agent may correct the block (and the
corresponding block in `mega_tutorial.sh`) and record the change as
`FIX_TUTORIAL`. The `Scope:` prose remains off-limits.

## Components and changes

1. **`scripts/tutorial_matcher.py`** -- extract the `Tutorial block:` section
   from each test's docstring (instead of the `write_tutorial_block(...)` call)
   and match it against `mega_tutorial.sh` blocks.

2. **e2e conftest** (`libs/mngr/imbue/mngr/e2e/conftest.py`) -- the `e2e`
   fixture captures the test's docstring into `docstring.txt` in the test output
   dir (replacing the `write_tutorial_block` -> `tutorial_block.txt` side
   effect). `write_tutorial_block` is removed once no call sites remain.

3. **`libs/mngr/imbue/mngr/utils/detail_renderer.py`** -- render `docstring.txt`
   under a `Docstring` heading (renamed from `Tutorial block`).

4. **TMR prompts** (`libs/mngr_tmr/.../prompt_assets/mapper.j2`, `reducer.j2`)
   -- generic, anchored on the docstring; drop the tutorial-1:1 framing; keep
   `FIX_TUTORIAL` as the single docstring-mutation exception.

5. **TMR recipe/report** (`recipe.py`, `prompts.py`, `report.py`) -- stop
   assuming e2e: do not unconditionally inject `--mngr-e2e-run-name` (it errors
   on non-e2e test packages); keep the `e2e/` artifact-path discovery as a
   fallback only.

6. **`sync-tutorial-to-e2e-tests` skill** -- emit the new docstring format and
   perform the crystallize-the-scope reasoning step.

7. **One-time migration** of all mngr release tests (`libs/mngr` + the provider/
   agent packages): move each `write_tutorial_block` block into the docstring
   `Tutorial block:` section (mechanical), and author the `Scope:` prose
   (thinking). Scope is bounded below by the tutorial block (for tutorial tests)
   and above by the existing test code, shrunk where it overlaps sibling tests
   in the same file. Non-tutorial tests use judgment with no lower bound; a test
   shrunk to triviality may be removed (tutorial tests may not, since the
   matcher requires every block to have a corresponding test).
