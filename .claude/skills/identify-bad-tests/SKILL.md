---
name: identify-bad-tests
argument-hint: [target_path]
description: Identify low-quality or fragile tests (weak/tautological assertions, missing cases, mock misuse, flakiness, mis-placement) under the $1 path
---

The argument `$1` is the path to scan. It may be an entire library (e.g. `libs/mngr`, or just the bare name `mngr`) or any subdirectory within one (e.g. `libs/mngr/imbue/mngr/cli`), so you can scope this skill narrowly to part of a library when that is all you care about.

Before doing anything else, resolve these two things from `$1` and state them explicitly:

- **The scan scope**: the directory tree you will examine. This is `$1` itself, resolved to a real path. (If `$1` is a bare library name like `mngr`, resolve it to `libs/mngr` or `apps/mngr` -- whichever exists.) You must only report findings for code under this path.
- **The containing library**: the project directory that owns the scan scope. Projects always live at `libs/<name>` or `apps/<name>`, so the containing library is exactly that two-component prefix of the scan-scope path (e.g. for a scan scope of `libs/mngr/imbue/mngr/cli`, the containing library is `libs/mngr`; for a scan scope of `libs/mngr`, it is `libs/mngr` itself). You need this both to gather context and to know where to write the output file -- so make sure you have identified it unambiguously before continuing.

Read the entire "# Testing" section of style_guide.md closely (in particular "High quality tests", "Testing without mocks", "Snapshot testing", "Test isolation", "Test quality", "Test organization", and "Types of tests"), and the containing library's non_issues.md, so you know the bar a test must clear. Then read the test files in the scan scope in full -- these are what you are reviewing, so the CLAUDE.md rule to skip `_test.py` and `test_*.py` files does NOT apply here. You don't need to read all of the production code up front; instead get a sense of what the containing library is meant to do (its README and docs), and drill into the specific production code a test exercises, plus the shared fixtures/helpers in the relevant `conftest.py`, `testing.py`, and `mock_*_test.py` files, whenever you need that detail to judge whether the test actually verifies the right behavior (e.g. to tell a real shared mock implementation from an ad-hoc fake).

Once you've gathered that context, please do the below.

Your task is to identify tests within the scan scope that are low-quality, fragile, or misleading -- tests that pass without actually establishing that the code is correct, that will break for reasons unrelated to real bugs, or that are placed/structured wrongly. A bad test is worse than no test: it spends CI time, it lulls readers into thinking a behavior is covered when it is not, and it has to be maintained. **The central question for every test is: would this test fail if the code under test had a real bug? If you cannot point to a specific bug the test would catch, it is a candidate.**

Focus on the semantic quality of the tests -- the things a linter or ratchet cannot see. Some anti-patterns (`unittest.mock` imports, `monkeypatch.setattr`, `time.sleep`, inline imports) are already enforced by the per-project `test_ratchets.py`; go look at the existing ratchet tests and do NOT re-report a raw occurrence that a ratchet already counts. You should still report the *semantic* problem when one of those patterns is being used to fake out behavior in a way that makes the test meaningless, but say what is wrong with the test, not merely "this uses a mock".

## What to look for

The style guide defines the bar; this is a checklist of the concrete shapes that violate it, to scan for. Examine every test in the scan scope, looking for:

- **Tautological or unfalsifiable assertions** -- asserting a constructor field equals the value just passed in, a constant against itself, or any value the test constructed rather than the *effect* of the operation under test.
- **"No exception raised" as the only check** -- the body calls the code but never asserts on the output or effect.
- **Tests of implementation rather than behavior** -- assertions on internal call order, private attributes, or how a result was computed, such that a behavior-preserving refactor would break them.
- **Error tests that don't pin the error** -- `pytest.raises(Exception)` (or a bare `except`) that never checks the specific error type or message.
- **Weak coverage-chasing assertions** -- `assert result is not None`, `assert len(x) > 0`, and similar, where the real contract is far more specific.
- **Missing cases** -- branchy or collection-handling code tested only on the happy path, with no empty / single / boundary / per-branch coverage.
- **Mock misuse and faking** -- hand-rolled fakes that don't inherit the interface they stand in for, mocks defined inline instead of in a shared `mock_*_test.py`, `types.SimpleNamespace` where a real object belongs, or mocking the very thing under test so the assertion only checks the mock.
- **Flakiness and isolation hazards** -- sleep/wall-clock synchronization instead of polling, non-unique IDs/constants, shared mutable state or order-dependence, touching the real `HOME` or other shared on-disk state, live network from unit/integration tests.
- **Wrong type, location, or marking** -- unit tests (`*_test.py`) that are slow or reach external resources; integration/acceptance/release tests in the wrong filename pattern or missing their `@pytest.mark.*`; or tests that use external resources (network, credentials, expensive infra) left unmarked as plain unit/integration tests rather than `@pytest.mark.acceptance`/`release`.
- **Organization and naming** -- classes used to group test functions, undescriptive test names, or `parametrize` mismatched to whether the cases truly share logic (split apart, or merge near-duplicate functions).
- **Snapshot misuse** -- hand-written "expected" values that duplicate the implementation's logic (so test and code drift together silently), oversized inline snapshots that should be hashed/file-based, or snapshots too opaque to review.

## How to evaluate each candidate

For each test (or assertion) you find, ask these three questions:

1. **Would it catch a real bug?** Construct the concrete bug -- a specific wrong value, a swapped branch, a dropped side effect -- that this test is supposed to guard against, and check whether the assertions would actually fail on it. If you cannot name such a bug, or the assertions would still pass with the bug present, it is a candidate. "It exercises the code" is not the same as "it verifies the code".
2. **Does it test behavior or implementation?** Would a legitimate, behavior-preserving refactor of the production code break this test? If yes, it is coupled to implementation details and is a candidate. The good version asserts on the observable effect (return value, persisted state, emitted output), not on how the result was produced.
3. **Will it fail for the wrong reasons?** Could this test fail (or pass) due to timing, ordering, shared state, environment, or non-unique identifiers rather than the correctness of the code? Flag anything that is non-deterministic, not self-isolating, or dependent on another test having run first.

## Reporting

**Err on the side of over-reporting.** It is fine to report a test you are not certain is bad. The cost of a false positive is low: if the test turns out to be fine, the remedy is a one-line note explaining why it is adequate, which is itself useful documentation. So when in doubt, report it.

For each finding, the `Recommendation` should be a concrete fix, e.g.: rewrite the assertion to check the operation's effect (with the specific value/snapshot to assert); add the missing empty/boundary/branch case; replace the inline fake with the shared `mock_*_test.py` implementation or a real object; make IDs unique with `uuid4().hex`; replace sleep-based synchronization with polling on a condition; move the test to the correct file/marker for its type; or split a class-grouped test into module-level `test_` functions. If a flagged test turns out to be adequate, recommend the brief clarifying comment that explains why (e.g. why this assertion is sufficient, or why this case is the only reachable one).

If you find a test that is flaky (passes and fails non-deterministically), call it out prominently -- per CLAUDE.md, flaky tests must be highlighted.

Do NOT report issues that are already covered by an existing FIXME.

Do NOT report issues that are highlighted as non-issues in non_issues.md.

Do NOT re-report a raw pattern occurrence that an existing ratchet already counts (report the semantic test-quality problem instead, if there is one).

After reviewing all the tests in the scan scope, think carefully about the most important and most misleading ones (a test that silently passes on a real bug is worse than one that is merely redundant).

Then put them, in order from most important to least important, into a markdown file in the *containing library's* "_tasks/bad-tests/" folder (make one if you have to) -- always the containing library's folder, even when the scan scope was a subdirectory, so the findings live where the other identify-* outputs and create-fixmes expect them. Name the file "<date>.md" (where you should get "date" by calling this precise command: "date +%Y-%m-%d-%T | tr : -")

For the format of the file, use the following:

```markdown
# Bad tests under <scan scope> (identified on <date>)
## 1. <Short description of the bad test>

Description: <detailed description, including file names, test function names, and line numbers, of what the test does, why it is bad (which of the three questions it fails), and what real bug it fails to catch or what unrelated reason it would break for>

Recommendation: <the concrete fix, or, if the test is actually adequate, the clarifying comment to add and what it should say>

Decision: Accept

## 2. <Short description of the bad test>

Description: <detailed description of the bad test, including file names, test function names, and line numbers where applicable>

Recommendation: <your recommendation for how to fix it, or the clarifying comment to add>

Decision: Accept

...
```

There's no need to commit when you're done (these files are gitignored). Just be sure to create the file in the right location with the right content.
