# Unabridged Changelog - overlay

Full, unedited changelog entries consolidated nightly from individual files in `libs/overlay/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-19

Added the `overlay` library: a total, dependency-free algebra for merging layered
configuration dicts. It owns the key-suffix operators (`__extend` / `__assign`), the
`Static*` atomic-value markers, the lower-level building blocks (`apply_extend`,
`extend_dict`, `combine_patches`), and the unified `merge` / `finalize` operations with
recursive narrowing detection (`would_assignment_narrow`). The algebra was extracted
verbatim from mngr's config layer (`key_resolver_primitives.py`, the merge/finalize half
of `key_resolver.py`, and the `Static*` / narrowing-predicate part of `data_types.py`);
mngr now depends on `overlay`. Structural parse errors raise `OverlayError` (which
mngr's `ConfigParseError` subclasses, preserving existing behavior).

Added the typed-node merge algebra as the new API (additive; the string-suffix API
above stays present, unchanged, pending the mngr migration). Three frozen node
wrappers (`Default` / `Assign` / `Extend`, in `nodes.py`) carry the operator in the
value's *type* instead of a key-string suffix. `lift` turns a suffix-keyed surface
dict into a node `Patch` (folding the within-layer "reset then add" once, up front); a
plain base dict with no suffix keys simply lifts to an all-`Default` patch, so `lift` is
used for a concrete base too. The node algebra (`node_merge.py`) provides `combine` / `finalize` /
`apply_extend` plus the public `merge` (raises `NarrowingError`, aggregating every
narrowing path) and `merge_narrowing_allowed` (returns the patch and the paths without
raising). Because the algebra never re-parses a field name or unwraps a payload to look
for an inner operator, stacked suffixes (`a__extend__assign`) are safe by construction:
they lift to a single wrapper on a literal field name and never reactivate.

Narrowing detection now reports the specific narrowed leaf path rather than the
containing field. A new `narrowing_paths` predicate (the path-collecting counterpart of
`would_assignment_narrow`) drives this: a same-keys dict whose nested value narrows
yields the deep leaf path (e.g. `commands.create.defaults.env`), while a dropped dict
key or a list/set narrowing still reports at the field. The raise/no-raise decision is
unchanged -- only the path strings are more precise.

Internal (no user-facing behavior change): the original string-suffix merge engine
(`combine_patches`, the string-suffix `merge` / `finalize`, and their private helpers)
is removed now that the typed-node algebra in `node_merge.py` is the sole engine. The
parallel plain-dict extend recursion (`merge.py`'s `apply_extend` / `extend_dict`) is
also removed: `node_merge.py` is now the single extend algebra, with a thin
`extend_plain_value` adapter that lifts a plain resolved value into the node engine for
the suffix-keyed-dict consumers (mngr's `key_resolver` / `common_opts`). The `merge.py`
module is now just the leaf-extend primitive `extend_aggregate_leaf` and the narrowing
predicates `would_assignment_narrow` / `narrowing_paths` that the node engine imports.
`extend_plain_value` names the dotted `field_path` when it rejects a contradictory
bare/`__assign` key at the top level of the extend body, matching the location the
removed plain-dict resolver surfaced.

Internal (no user-facing behavior change): the low-cohesion `merge.py` grab-bag is split
by concern. The value-level narrowing predicates (`would_assignment_narrow` /
`narrowing_paths`) move verbatim to a new `narrowing.py` module, and the leaf-extend
primitive `extend_aggregate_leaf` moves into `node_merge.py` next to the other extend
helpers (`apply_extend` / `combine_extend_payloads`). `merge.py` is deleted. Pure
move/rename: function bodies are unchanged, only their module home and import lines move.
