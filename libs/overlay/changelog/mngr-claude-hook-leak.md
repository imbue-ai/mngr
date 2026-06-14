Added the `overlay` library: a total, dependency-free algebra for merging layered
configuration dicts. It owns the key-suffix operators (`__extend` / `__assign`), the
`Static*` atomic-value markers, the lower-level building blocks (`apply_extend`,
`extend_dict`, `combine_patches`), and the unified `merge` / `finalize` operations with
recursive narrowing detection (`would_assignment_narrow`). The algebra was extracted
verbatim from mngr's config layer (`key_resolver_primitives.py`, the merge/finalize half
of `key_resolver.py`, and the `Static*` / narrowing-predicate part of `data_types.py`);
mngr now depends on `overlay`. Structural parse errors raise `OverlayError` (which
mngr's `ConfigParseError` subclasses, preserving existing behavior).
