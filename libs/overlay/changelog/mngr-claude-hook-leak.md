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
dict into a node `Patch` (folding the within-layer "reset then add" once, up front);
`lift_concrete` wraps a plain base dict as an all-`Default` patch for "merge against a
concrete base". The node algebra (`node_merge.py`) provides `combine` / `finalize` /
`apply_extend` plus the public `merge` (raises `NarrowingError`, aggregating every
narrowing path) and `merge_narrowing_allowed` (returns the patch and the paths without
raising). Because the algebra never re-parses a field name or unwraps a payload to look
for an inner operator, stacked suffixes (`a__extend__assign`) are safe by construction:
they lift to a single wrapper on a literal field name and never reactivate.
