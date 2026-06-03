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

Your task is to identify tests within the scan scope that are low-quality, fragile, or misleading -- tests that pass without establishing that the code is correct, that break for reasons unrelated to real bugs, or that are placed or structured wrongly. A bad test is worse than no test: it costs CI time and maintenance, and it lulls readers into thinking a behavior is covered when it is not.

The style guide's "# Testing" section already catalogs what makes a test good -- do not restate it; your job is to apply it. Focus on semantic quality, the things a linter or ratchet cannot see. (`unittest.mock`, `monkeypatch.setattr`, `time.sleep`, and inline imports are already counted by the per-project `test_ratchets.py`; check those ratchets and do not re-report a raw occurrence. Do report the semantic damage when such a pattern makes a test meaningless -- e.g. a mock that fakes out the very thing under test -- but say what is wrong with the test, not merely that it "uses a mock".)

## How to judge each test

Judge every test along two axes.

**Does it actually verify behavior?** This is the heart of it. For each test or assertion, ask:

1. **Would it catch a real bug?** Name the concrete bug -- a specific wrong value, a swapped branch, a dropped side effect -- the test should guard against, and check that its assertions would actually fail on it. If you cannot, or they would still pass, it is a candidate. Exercising the code is not the same as verifying it. (Tautological assertions, "it didn't raise" with no check on the result, and loose coverage-chasing assertions all fail here.)
2. **Behavior or implementation?** Would a behavior-preserving refactor break it? Then it is coupled to implementation details (internal call order, private attributes, how a result was computed) and is a candidate; good tests assert on observable effects.
3. **Could it fail for the wrong reasons?** Sleep-based synchronization, non-unique IDs, shared state, order-dependence, real network access, or anything not self-isolating makes it flaky -- flag it.

**Is it well-formed per the style guide?** Flag structural divergences the guide defines: wrong test type, location, or marker for the dependencies it actually uses; classes used to group test functions; undescriptive names; misuse of `parametrize`; missing edge / branch / empty-collection cases; and snapshot misuse (hand-written expected values that just duplicate the code, or oversized inline snapshots that should be hashed).

## Reporting

**Err on the side of over-reporting.** It is fine to report a test you are not certain is bad. The cost of a false positive is low: if the test turns out to be fine, the remedy is a one-line note explaining why it is adequate, which is itself useful documentation. So when in doubt, report it.

For each finding, the `Recommendation` should be a concrete fix, e.g.: rewrite the assertion to check the operation's effect (with the specific value/snapshot to assert); add the missing empty/boundary/branch case; replace the inline fake with the shared `mock_*_test.py` implementation or a real object; make IDs unique with `uuid4().hex`; replace sleep-based synchronization with polling on a condition; move the test to the correct file/marker for its type; or split a class-grouped test into module-level `test_` functions. If a flagged test turns out to be adequate, recommend the brief clarifying comment that explains why (e.g. why this assertion is sufficient, or why this case is the only reachable one).

If you find a test that is flaky (passes and fails non-deterministically), call it out prominently -- per CLAUDE.md, flaky tests must be highlighted.

Do NOT report issues that are already covered by an existing FIXME.

Do NOT report issues that are highlighted as non-issues in non_issues.md.

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
