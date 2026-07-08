"""The typed-node merge algebra: lift, combine, finalize, and the public API.

This is the self-contained typed-node engine, and the single extend algebra. It owns the
leaf-extend primitive (``extend_aggregate_leaf``) alongside its own ``apply_extend`` /
``combine_extend_payloads``, and imports the value-level narrowing predicate
(``narrowing_paths``) from ``narrowing.py`` to expand its recorded assign drops.
``extend_plain_value`` is the mngr-boundary adapter that lifts a plain resolved value into
this engine so the suffix-keyed-dict consumers (``key_resolver`` / ``common_opts``) share
the one extend algebra rather than a parallel plain-dict recursion.

The operator lives in the node *type* (``Default`` / ``Assign`` / ``Extend``), so
the algebra dispatches on type and rewrites the outermost wrapper but never unwraps
a payload to look for an inner operator -- which is what makes stacked suffixes safe
by construction (see ``nodes`` and the spec).

Pipeline:

- ``lift`` turns a suffix-keyed surface dict into a node ``Patch`` (one node per
  field, with the within-layer "reset then add" already folded in).
- ``combine`` folds ``higher`` over ``lower`` (both node patches), pure/recursive,
  collecting ``AssignDrop`` candidates for the narrowing filter.
- ``finalize`` collapses a node patch to a plain dict.
- ``merge`` / ``merge_narrowing_allowed`` are the public API over the private
  ``_merge`` (combine + narrowing filter); ``merge`` raises ``NarrowingError``.
"""

from collections.abc import Mapping
from typing import Any

from imbue.overlay.errors import NarrowingError
from imbue.overlay.errors import OverlayError
from imbue.overlay.markers import is_static_marker
from imbue.overlay.narrowing import narrowing_paths
from imbue.overlay.nodes import Assign
from imbue.overlay.nodes import Default
from imbue.overlay.nodes import Extend
from imbue.overlay.nodes import Node
from imbue.overlay.nodes import Patch
from imbue.overlay.nodes import is_assign_kind
from imbue.overlay.operators import ASSIGN_SUFFIX
from imbue.overlay.operators import EXTEND_SUFFIX
from imbue.overlay.operators import assign_bare_key
from imbue.overlay.operators import bare_key
from imbue.overlay.operators import check_no_conflicting_assign
from imbue.overlay.operators import is_assign_key
from imbue.overlay.operators import is_extend_key

# A candidate assign drop recorded by ``combine`` for the narrowing filter:
# ``(lower_payload, higher_payload, dotted_path)``. ``combine`` appends one for
# every ``Default`` that overrides a lower node; ``_merge`` then expands each into the
# specific narrowed leaf paths (via ``narrowing_paths`` on the finalized payloads).
AssignDrop = tuple[Any, Any, str]

# The aggregate leaf types a payload may be (besides scalars / nested patches).
# ``Static*`` markers subclass these and so are covered.
_AGGREGATE_LEAF_TYPES = (list, tuple, set, frozenset)


def _is_patch_dict(value: Any) -> bool:
    """Return True if ``value`` should be descended into as a nested ``Patch``.

    A plain dict is a patch; a ``Static*`` aggregate (incl. ``StaticDict``, a dict
    subclass) is an **atomic leaf** -- carried whole, never lifted/lowered into nodes
    -- so it keeps its narrowing exemption rather than dissolving into a patch.
    """
    return isinstance(value, dict) and not is_static_marker(value)


# =============================================================================
# lift -- surface syntax (suffix-keyed dict) -> node Patch
# =============================================================================


def lift(raw: dict[str, Any]) -> Patch:
    """Turn a suffix-keyed surface dict into a node-valued ``Patch``.

    For each bare field name, gather its bare / ``__assign`` / ``__extend`` forms
    and produce exactly one node:

    - bare and ``__assign`` together -> ``OverlayError`` (contradictory assigns).
    - assign form only -> ``Default`` (bare) or ``Assign`` (``__assign``).
    - extend form only -> ``Extend``.
    - assign form and extend form together -> one node of the assign's kind whose
      payload is the extend applied onto the assign payload (the within-layer
      "reset then add", resolved here, once, up front; order-independent w.r.t.
      source key order).

    Values are payload-lifted recursively (a nested dict becomes ``lift(dict)``).
    Only the outermost suffix is stripped, once: a stacked ``a__extend__assign``
    lifts to field name ``a__extend`` with an ``Assign`` wrapper -- a literal name
    that is never re-parsed downstream.
    """
    bare_assigns: dict[str, Any] = {}
    explicit_assigns: dict[str, Any] = {}
    extends: dict[str, Any] = {}
    for key, value in raw.items():
        payload = lift(value) if _is_patch_dict(value) else value
        if is_extend_key(key):
            extends[bare_key(key)] = payload
        elif is_assign_key(key):
            explicit_assigns[assign_bare_key(key)] = payload
        else:
            bare_assigns[key] = payload

    conflicts = sorted(set(bare_assigns) & set(explicit_assigns))
    if conflicts:
        names = ", ".join(conflicts)
        raise OverlayError(
            f"Conflicting assignment: field(s) [{names}] have both a bare key and a "
            f"'{ASSIGN_SUFFIX}' key in the same layer. Use exactly one of bare assign or "
            f"'{ASSIGN_SUFFIX}' (assign without the narrowing check), not both."
        )

    patch: Patch = {}
    for field, payload in bare_assigns.items():
        patch[field] = _fold_assign_with_extend(Default, payload, field, extends)
    for field, payload in explicit_assigns.items():
        patch[field] = _fold_assign_with_extend(Assign, payload, field, extends)
    for field, payload in extends.items():
        if field not in bare_assigns and field not in explicit_assigns:
            patch[field] = Extend(payload)
    return patch


def _fold_assign_with_extend(
    kind: type[Default] | type[Assign],
    assign_payload: Any,
    field: str,
    extends: dict[str, Any],
) -> Node:
    """Build the assign-kind node for ``field``, folding a same-layer ``__extend``
    payload onto the assign payload when one exists (within-layer reset-then-add)."""
    if field not in extends:
        return kind(assign_payload)
    return kind(apply_extend(assign_payload, extends[field], field))


def lower(patch: Patch) -> dict[str, Any]:
    """Lower a node ``Patch`` back to a suffix-keyed surface dict (the inverse of
    ``lift``): ``Default`` -> bare key, ``Assign`` -> ``key__assign``, ``Extend`` ->
    ``key__extend``, recursing into nested ``Patch`` payloads.

    Used at the boundary where a consumer stores or carries an unresolved patch as
    plain suffix-keyed data (e.g. mngr keeps deferred ``settings_overrides`` /
    ``create_templates`` markers as JSON-able suffix strings rather than node
    objects). ``lift(lower(patch))`` round-trips a combined patch faithfully -- a
    ``Default`` never carries an operator-suffixed field name (``lift`` could not
    have produced one), and ``Assign`` / ``Extend`` re-emit their own suffix, so a
    re-``lift`` reproduces the same nodes and never reactivates a stray suffix.
    """
    result: dict[str, Any] = {}
    for field, node in patch.items():
        lowered = lower(node.payload) if _is_patch_dict(node.payload) else node.payload
        if isinstance(node, Extend):
            result[f"{field}{EXTEND_SUFFIX}"] = lowered
        elif isinstance(node, Assign):
            result[f"{field}{ASSIGN_SUFFIX}"] = lowered
        else:
            result[field] = lowered
    return result


# =============================================================================
# apply_extend / combine_extend_payloads -- payload-level extend
# =============================================================================


def apply_extend(
    current_payload: Any,
    extend_payload: Any,
    field_path: str,
    assign_drops: list[AssignDrop] | None = None,
) -> Any:
    """Apply ``extend_payload`` onto ``current_payload``, returning a new payload.

    Operates on payloads (a leaf or a ``Patch``). A ``Patch`` target recurses via
    ``combine`` (so the extend's nested nodes merge onto the current patch in the
    right precedence order); a list/tuple target concatenates; a set/frozenset
    target unions; a scalar target is an error. Extend-against-absent
    (``current_payload is None``) acts as assign -- the extend payload becomes the
    value -- but the shape must still be an aggregate or a patch.

    ``assign_drops`` threads into the recursive ``combine`` so a bare (``Default``)
    assign nested *inside* this extend payload that drops a lower aggregate is still
    recorded for the narrowing filter (the recursive-narrowing case): an extend never
    narrows at its own level, but a nested assign within it can.

    A ``Static*`` marker is an atomic leaf everywhere else in the algebra (``_is_patch_dict``
    excludes it), so as an extend payload it replaces the base wholesale rather than being
    recursed/concatenated, and a ``Static*`` base is never descended into.
    """
    if is_static_marker(extend_payload):
        return extend_payload
    if current_payload is None:
        if _is_patch_dict(extend_payload):
            return combine({}, extend_payload, path=(field_path,), assign_drops=assign_drops)
        if not isinstance(extend_payload, _AGGREGATE_LEAF_TYPES):
            raise OverlayError(
                f"__extend on field '{field_path}' requires a list, tuple, dict, or set value; "
                f"got: {type(extend_payload).__name__}"
            )
        return extend_payload
    if _is_patch_dict(current_payload):
        if not _is_patch_dict(extend_payload):
            raise OverlayError(
                f"__extend on field '{field_path}' (dict) requires a JSON object value; "
                f"got: {type(extend_payload).__name__}"
            )
        return combine(current_payload, extend_payload, path=(field_path,), assign_drops=assign_drops)
    return extend_aggregate_leaf(current_payload, extend_payload, field_path)


def combine_extend_payloads(
    lower_payload: Any,
    higher_payload: Any,
    field_path: str,
    assign_drops: list[AssignDrop] | None = None,
) -> Any:
    """Combine two ``Extend`` payloads (neither resolved against a base yet).

    Both payloads come from ``Extend`` nodes for the same field. A ``Patch`` pair
    recurses via ``combine`` (threading ``assign_drops`` so a nested assign that
    drops a lower aggregate is still recorded); list/tuple pairs concatenate;
    set/frozenset pairs union. Incompatible shapes are an error. The result is still
    an unresolved extend payload (stays in an ``Extend`` node).

    A ``Static*`` marker on either side is an atomic leaf, so the higher-precedence
    payload wins outright rather than the two being merged.
    """
    if is_static_marker(lower_payload) or is_static_marker(higher_payload):
        return higher_payload
    if _is_patch_dict(lower_payload) and _is_patch_dict(higher_payload):
        return combine(lower_payload, higher_payload, path=(field_path,), assign_drops=assign_drops)
    if isinstance(lower_payload, (list, tuple)) and isinstance(higher_payload, (list, tuple)):
        merged = list(lower_payload) + list(higher_payload)
        return tuple(merged) if isinstance(lower_payload, tuple) else merged
    if isinstance(lower_payload, (set, frozenset)) and isinstance(higher_payload, (set, frozenset, list, tuple)):
        merged_set = set(lower_payload) | set(higher_payload)
        return frozenset(merged_set) if isinstance(lower_payload, frozenset) else merged_set
    raise OverlayError(
        f"Cannot combine __extend values for field '{field_path}': incompatible shapes "
        f"({type(lower_payload).__name__} and {type(higher_payload).__name__})."
    )


def extend_aggregate_leaf(current: Any, extend_payload: Any, field_path: str) -> Any:
    """Extend a non-dict aggregate leaf (``current``) by ``extend_payload`` and return it.

    The shape-checked leaf branch of the extend algebra: a list/tuple concatenates, a
    set/frozenset unions, and a scalar target is an error. ``apply_extend`` handles the
    ``current is None`` and dict/``Patch`` cases before reaching here.
    """
    if isinstance(current, (list, tuple)):
        if not isinstance(extend_payload, (list, tuple)):
            raise OverlayError(
                f"__extend on field '{field_path}' (list/tuple) requires a JSON array value; "
                f"got: {type(extend_payload).__name__}"
            )
        merged = list(current) + list(extend_payload)
        return tuple(merged) if isinstance(current, tuple) else merged
    if isinstance(current, (set, frozenset)):
        if not isinstance(extend_payload, (list, tuple, set, frozenset)):
            raise OverlayError(
                f"__extend on field '{field_path}' (set) requires a JSON array value; "
                f"got: {type(extend_payload).__name__}"
            )
        merged_set = set(current) | set(extend_payload)
        return frozenset(merged_set) if isinstance(current, frozenset) else merged_set
    raise OverlayError(
        f"__extend on field '{field_path}' is not valid: target field is a scalar "
        f"({type(current).__name__}); use bare assignment instead."
    )


# =============================================================================
# combine -- cross-layer node-patch combine (higher over lower)
# =============================================================================


def combine(
    lower: Patch,
    higher: Patch,
    *,
    path: tuple[str, ...] = (),
    assign_drops: list[AssignDrop] | None = None,
) -> Patch:
    """Combine two node patches, ``higher`` over ``lower``, pure/recursive/associative.

    Per key: a key present in only one side carries through unchanged; a key in both
    dispatches to ``combine_nodes``. ``assign_drops`` (if provided) collects the
    ``(lower_payload, higher_payload, dotted_path)`` candidate for every ``Default``
    that overrides a lower node, for the later narrowing filter.
    """
    result: Patch = {}
    for key, node in lower.items():
        if key not in higher:
            result[key] = node
    for key, higher_node in higher.items():
        if key in lower:
            result[key] = combine_nodes(lower[key], higher_node, path + (key,), assign_drops)
        else:
            result[key] = higher_node
    return result


def combine_nodes(
    lower_node: Node,
    higher_node: Node,
    path: tuple[str, ...],
    assign_drops: list[AssignDrop] | None,
) -> Node:
    """Combine two nodes for the same field, ``higher_node`` over ``lower_node``.

    - higher is assign-kind (``Default`` / ``Assign``): higher wins wholesale; lower
      dropped. ``Default`` records an ``AssignDrop`` candidate; ``Assign`` records
      nothing (it suppresses the narrowing check).
    - higher is ``Extend`` over an assign-kind lower: same kind as lower, payload
      ``apply_extend(lower_payload, higher_payload)``. Never narrows.
    - higher is ``Extend`` over ``Extend``: ``Extend(combine_extend_payloads(...))``;
      stays deferred (no base yet).
    """
    if is_assign_kind(higher_node):
        # Narrowing is recorded only when a Default replaces a lower *assign-kind* node.
        # A lower Extend is an unresolved increment, not a set value, so a higher Default
        # over it is an authoritative assign, not a narrowing: recording it would raise
        # false positives for the normal "lower extends, higher sets" case, and would make
        # the narrowing result depend on fold grouping (the compared base differs between
        # (L*M)*H and L*(M*H)), breaking combine's associativity.
        if isinstance(higher_node, Default) and is_assign_kind(lower_node) and assign_drops is not None:
            assign_drops.append((lower_node.payload, higher_node.payload, ".".join(path)))
        return higher_node
    # higher_node is Extend.
    field_path = ".".join(path)
    if is_assign_kind(lower_node):
        extended = apply_extend(lower_node.payload, higher_node.payload, field_path, assign_drops)
        return type(lower_node)(extended)
    # lower_node is Extend too.
    return Extend(combine_extend_payloads(lower_node.payload, higher_node.payload, field_path, assign_drops))


# =============================================================================
# finalize -- collapse a node patch to a plain dict
# =============================================================================


def finalize(patch: Patch) -> dict[str, Any]:
    """Collapse a node patch to a plain dict: every node (``Default`` / ``Assign`` /
    ``Extend``) becomes ``finalize_payload(payload)``.

    A surviving ``Extend`` collapsing to its payload is correct (extend-against-
    nothing = assign), so there is no assertion that no ``Extend`` remains.
    """
    return {key: finalize_payload(node.payload) for key, node in patch.items()}


def finalize_payload(payload: Any) -> Any:
    """Collapse a payload: recurse for a ``Patch``, return the leaf otherwise (a
    ``Static*`` aggregate is an atomic leaf, returned whole)."""
    if _is_patch_dict(payload):
        return finalize(payload)
    return payload


# =============================================================================
# extend_plain_value -- plain-dict boundary into the node extend algebra
# =============================================================================


def extend_plain_value(current: Any, extend: Any, field_path: str) -> Any:
    """Apply a single ``__extend`` (``extend``) onto a plain resolved value (``current``).

    The mngr-boundary plain-value entry into the node extend algebra: a thin
    lift/finalize adapter so a suffix-keyed-dict consumer (``key_resolver`` /
    ``common_opts``) shares the one node ``apply_extend`` rather than a parallel
    plain-dict recursion. Both a dict ``current`` and a dict ``extend`` are lifted via
    ``lift`` (so a nested ``key__extend`` recurses and a nested bare key assigns) -- an
    ``__extend`` marker in ``current`` is honored too, since extend-against-nothing just
    yields the value; leaf values pass straight through to ``apply_extend``. ``current is
    None`` makes the extend act as assign. ``OverlayError``s propagate (callers wrap).

    A conflicting bare/``__assign`` key at the top level of a dict ``extend`` is rejected
    with the dotted ``field_path`` in the message (``check_no_conflicting_assign`` before
    lifting), matching the location the plain-dict resolver used to surface; ``lift``'s own
    engine-level check still covers any conflict nested deeper, without a location.
    """
    cur = lift(current) if isinstance(current, Mapping) else current
    if isinstance(extend, Mapping):
        check_no_conflicting_assign(extend, field_path)
        ext: Any = lift(extend)
    else:
        ext = extend
    return finalize_payload(apply_extend(cur, ext, field_path))


# =============================================================================
# Public API: _merge / merge / merge_narrowing_allowed
# =============================================================================


def _merge(lower: Patch, higher: Patch) -> tuple[Patch, list[str]]:
    """Combine ``higher`` over ``lower`` and return the patch plus narrowing paths.

    Collects ``AssignDrop`` candidates during ``combine``, then expands each into the
    specific narrowed leaf paths via ``narrowing_paths`` on the **finalized** payloads,
    seeded with the recorded dotted field path as the prefix. ``Static*`` and superset
    overrides are exempt (yield no paths); a dropped dict key or a list/set narrowing
    reports at the field path, while a nested dict value narrowing reports the deep leaf
    path. Never raises for narrowing (only a structural ``OverlayError`` from ``combine``
    can propagate).
    """
    assign_drops: list[AssignDrop] = []
    merged = combine(lower, higher, assign_drops=assign_drops)
    narrowings: list[str] = []
    for lower_payload, higher_payload, dotted in assign_drops:
        narrowings.extend(narrowing_paths(finalize_payload(lower_payload), finalize_payload(higher_payload), dotted))
    return merged, narrowings


def merge(lower: Patch, higher: Patch) -> Patch:
    """Combine ``higher`` over ``lower``, raising ``NarrowingError`` on any narrowing.

    The strict default: every narrowing path in this combine is aggregated into one
    ``NarrowingError``. Callers that want to surface (or discard) narrowings instead
    of raising use ``merge_narrowing_allowed``.
    """
    merged, narrowings = _merge(lower, higher)
    if narrowings:
        raise NarrowingError(narrowings)
    return merged


def merge_narrowing_allowed(lower: Patch, higher: Patch) -> tuple[Patch, list[str]]:
    """Combine ``higher`` over ``lower`` without raising; return the patch and the
    narrowing paths for the caller to surface or discard."""
    return _merge(lower, higher)
