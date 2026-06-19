---
name: identify-suspicious-edge-cases
argument-hint: [target_path]
description: Identify suspicious edge-case handling (over-broad catches, fallback else branches, defensive guards) under the $1 path
---

The argument `$1` is the path to scan. It may be an entire library (e.g. `libs/mngr`, or just the bare name `mngr`) or any subdirectory within one (e.g. `libs/mngr/imbue/mngr/cli`), so you can scope this skill narrowly to part of a library when that is all you care about.

Before doing anything else, resolve these two things from `$1` and state them explicitly:

- **The scan scope**: the directory tree you will examine. This is `$1` itself, resolved to a real path. (If `$1` is a bare library name like `mngr`, resolve it to `libs/mngr` or `apps/mngr` -- whichever exists.) You must only report findings for code under this path.
- **The containing library**: the project directory that owns the scan scope. Projects always live at `libs/<name>` or `apps/<name>`, so the containing library is exactly that two-component prefix of the scan-scope path (e.g. for a scan scope of `libs/mngr/imbue/mngr/cli`, the containing library is `libs/mngr`; for a scan scope of `libs/mngr`, it is `libs/mngr` itself). You need this both to gather context and to know where to write the output file -- so make sure you have identified it unambiguously before continuing.

Go gather all the context for the containing library (per instructions in CLAUDE.md). Even when the scan scope is a small subdirectory, you still need the whole containing library's context (style guide, primitives, data_types, interfaces, utils) to judge whether an edge case is handled correctly. Be sure to read the containing library's non_issues.md as well, and read the error-handling and control-flow sections of style_guide.md (the "If/elif/else", "Match statements with assert_never", "Exception hierarchy", and "Try/except" sections in particular).

Once you've gathered that context, please do the below (and commit when you're finished).

Your task is to identify suspicious edge-case handling within the scan scope. The motivation is that defensively written code tends to *over-handle* edge cases: catching errors that should be allowed to crash, adding fallback `else` branches that paper over states that should be impossible, and inserting defensive guards that mask bugs. This skill is an intermittent cleanup pass to counteract that tendency. **The default stance is suspicion: assume each edge-case handler is unjustified until you can articulate not just that *some* handling must be there, but that *this specific logic* is the right way to handle the case.** It will often be true that the edge case needs handling of some kind; that is not enough. The bar is that the chosen behavior (this default, this caught type, this fallback value, this early return) is demonstrably the correct response to the case, not merely a plausible one.

## What to look for

Examine every place where the code handles a branch or failure that may not need handling, including:

- **`else` clauses in if/elif chains** (and the final `else` after a chain). Is the else genuinely reachable? If it represents a "this should never happen" case, the style guide says it should `raise`, not silently fall through to a default or `pass`. If the chain branches on an enum or other matchable value, a `match` with `assert_never` would let the type checker prove exhaustiveness instead.
- **`except` clauses.** Is the caught exception something we actually want to recover from, or are we swallowing a real bug? Is the caught type as narrow as possible? Does the `except` span more than the single statement that can raise? Could the handler hide a failure and let execution continue with bad state? Per the style guide: prefer to crash rather than catch.
- **Fallback default values**: `dict.get(key, default)`, `getattr(obj, name, default)`, `next(it, default)`, `or`-defaults (`x = maybe_none or fallback`), and similar. Is the default ever actually used in a legitimate flow, or does it just convert a missing/None value into a plausible-but-wrong one?
- **Defensive guards**: `if x is None: return ...`, `if not items: return ...`, `hasattr(...)`/`isinstance(...)` checks, early returns that handle "shouldn't happen" inputs. Does the type system already guarantee the guard is unnecessary? Would removing the guard surface a real bug instead of hiding it?
- **Broadened return types** (returning `None` on failure instead of raising, returning empty collections to signal an error, sentinel values).
- **Optional (`Something | None`) types.** Every `| None` is a branch the code is forced to handle somewhere, so treat each one as a candidate in its own right. Ask whether the `None` is genuinely reachable and meaningful, or whether the type is `| None` only because it was convenient (e.g. a field initialized to `None` and filled in later, an argument that defaults to `None`, a lookup that "might" miss but never does in practice). If the value is always present by the time it is used, the control flow can often be refactored so the type is just `Something` -- constructing the object with the value already set, splitting one type into "before" and "after" variants, or restructuring so the producer hands the consumer a non-optional value. Eliminating the `| None` deletes the downstream `if x is None` handling entirely and lets the type checker (ty) prove the value exists, rather than every call site having to re-handle a `None` that cannot occur. Flag optionals whose `None` case is never legitimately hit, or whose handling silently papers over a missing value.

## How to evaluate each candidate

For each edge-case handler you find, ask these three questions:

1. **Is the logic semantically unassailable?** Be suspicious by default. Can you construct a precise argument for exactly which real inputs reach this branch *and* why this particular handling is the right response to them (rather than, say, crashing, raising, or handling the case differently)? Showing that the case can occur is not enough on its own; the chosen behavior must be the correct one. If you cannot make that argument, it is a candidate. Vague justifications ("just in case", "for safety", "to be defensive") are red flags, not reasons.
2. **Can the case be avoided entirely?** Could restructuring the control flow remove the branch (e.g. converting an if/elif/else over an enum into a `match` with `assert_never`)? Could the type checker (ty) make the case unrepresentable, so the guard becomes dead code you can delete? Prefer making bad states unrepresentable over handling them.
3. **Could it silently produce wrong output?** The worst handlers are the ones that turn a loud failure into a quiet incorrect result: a swallowed exception that lets the caller proceed with partial data, a fallback default that flows downstream as if it were real, an `else` that returns a placeholder. Flag anything where, if the "impossible" case did occur, the program would keep running and produce a wrong answer instead of crashing.

## Reporting

**Err on the side of over-reporting.** It is fine to report a handler you are not certain is wrong. The cost of a false positive here is low and the benefit is high, because the remedy for a *correct* handler is not "delete it" but "add a comment explaining why the logic must be this way". That comment is itself valuable: it converts an unexplained edge case into documented, intentional logic, and saves the next reader (human or agent) from re-deriving the justification. So when in doubt, report it.

For each finding, the `Recommendation` should fall into one of two shapes:

- If the handler looks genuinely unjustified, avoidable, or capable of silent wrong output: recommend the concrete structural fix (remove the catch and let it crash, replace the chain with `match`/`assert_never`, tighten the exception type, delete the guard the type system already guarantees, raise instead of returning a fallback, etc.).
- If the handler turns out to be correct and necessary: recommend adding a brief inline comment that states precisely why this case can occur and why this handling is the desired behavior. (This is the remedy for an over-report.)

Do NOT report issues that are already covered by an existing FIXME.

Do NOT report issues that are highlighted as non-issues in non_issues.md.

After reviewing all the code in the scan scope, think carefully about the most important and most suspicious edge-case handlers.

Then put them, in order from most important to least important, into a markdown file in the *containing library's* "_tasks/suspicious-edge-cases/" folder (make one if you have to) -- always the containing library's folder, even when the scan scope was a subdirectory, so the findings live where the other identify-* outputs and create-fixmes expect them. Name the file "<date>.md" (where you should get "date" by calling this precise command: "date +%Y-%m-%d-%T | tr : -")

For the format of the file, use the following:

```markdown
# Suspicious edge cases under <scan scope> (identified on <date>)
## 1. <Short description of the suspicious edge case>

Description: <detailed description, including file names and line numbers, of what is handled, why it is suspicious (which of the three questions it fails), and what would happen if the "impossible" case actually occurred>

Recommendation: <either the concrete structural fix, or, if the logic is correct, the clarifying comment to add and what it should say>

Decision: Accept

## 2. <Short description of the suspicious edge case>

Description: <detailed description of the suspicious edge case, including file names and line numbers where applicable>

Recommendation: <your recommendation for how to fix it, or the clarifying comment to add>

Decision: Accept

...
```

There's no need to commit when you're done (these files are gitignored). Just be sure to create the file in the right location with the right content.
