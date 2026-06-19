"""Merge logic for settings files owned by an *external* AI CLI (Claude Code, antigravity).

mngr bakes a per-agent ``settings.json`` that the external CLI reads directly, folding the
agent type's ``settings_overrides`` onto a base (the user's synced home settings + mngr's
own additions). Merge intent on this path cannot use mngr's internal ``__extend`` /
``__assign`` leaf suffixes: the external CLI does not understand them and would treat
``permissions__extend`` as a junk literal key. So intent is declared in a single top-level
``__mngr_merge`` map (dotted key path -> ``"extend"`` | ``"assign"``) that the external CLI
silently ignores.

This module is the one place that knows that surface. It is self-contained -- it builds on
the generic overlay algebra and the shared narrowing-message skeleton, but never on mngr's
config-tree wiring. Callers drive it: ``key_resolver`` desugars at config-load, the
claude/antigravity plugins fold at provision via ``apply_settings_patch``, and ``mngr
config extend|assign`` writes ``__mngr_merge`` directives.

``__mngr_merge`` keys are dotted paths, so a settings key that contains a *literal* dot
(e.g. an MCP server name like ``my.server``) cannot be targeted; such a directive fails the
existence check, and the auto-generated remediation skips it rather than mis-advising.
"""

from collections.abc import Mapping
from typing import Any
from typing import Final

from imbue.mngr.config.overlay_merge import build_settings_narrowing_message
from imbue.mngr.errors import ConfigParseError
from imbue.overlay.markers import is_static_marker
from imbue.overlay.node_merge import finalize
from imbue.overlay.node_merge import lift
from imbue.overlay.node_merge import merge_narrowing_allowed
from imbue.overlay.operators import ASSIGN_SUFFIX
from imbue.overlay.operators import EXTEND_SUFFIX
from imbue.overlay.operators import assign_bare_key
from imbue.overlay.operators import bare_key
from imbue.overlay.operators import is_assign_key
from imbue.overlay.operators import is_extend_key

# Top-level key carrying the Claude-compatible merge-operator surface for a
# ``settings_overrides`` patch. Vanilla Claude/agy ignore it; mngr reads it.
MNGR_MERGE_KEY: Final[str] = "__mngr_merge"

# The operator words accepted in a ``__mngr_merge`` map, mapped to the internal leaf suffix
# each desugars to. ``extend`` merges onto the base (what the external CLI does natively
# across layers); ``assign`` replaces without the narrowing guard. A bare key (absent from
# the map) stays a narrowing-checked assign.
OP_SUFFIXES: Final[dict[str, str]] = {"extend": EXTEND_SUFFIX, "assign": ASSIGN_SUFFIX}

# Sentinel distinguishing "key absent" from a real ``None`` value when walking a patch.
_MISSING: Final = object()


# =============================================================================
# __mngr_merge surface -> internal suffix form (desugar), run at config-load
# =============================================================================


def _suffix_op_word(suffix: str) -> str:
    """Map an internal leaf suffix back to its ``__mngr_merge`` operator word."""
    return {EXTEND_SUFFIX: "extend", ASSIGN_SUFFIX: "assign"}[suffix]


def _reject_internal_keys(subtree: Any, rel: tuple[str, ...], location: str) -> None:
    """Raise if a suffix key (or a non-root ``__mngr_merge``) appears under settings_overrides.

    The subtree must express merge intent only through the single top-level ``__mngr_merge``
    map; raw ``__extend`` / ``__assign`` leaf suffixes are rejected (they would survive into
    the external CLI's settings.json as junk keys), and a nested ``__mngr_merge`` is rejected
    because the map is only meaningful at the patch root. ``rel`` is the path relative to the
    root and ``location`` names the owning agent type's ``settings_overrides``, for the message.
    """
    if not isinstance(subtree, Mapping):
        return
    for key, value in subtree.items():
        rel_dotted = ".".join(rel + (key,))
        if is_extend_key(key) or is_assign_key(key):
            bare = bare_key(key) if is_extend_key(key) else assign_bare_key(key)
            op = "extend" if is_extend_key(key) else "assign"
            bare_dotted = ".".join(rel + (bare,))
            raise ConfigParseError(
                f"`{rel_dotted}` in {location} uses an operator suffix, which is not allowed here: "
                f"Claude Code does not recognise it and would treat it as a literal key in your "
                f"settings.json. Declare the merge in a top-level `__mngr_merge` map instead, e.g. "
                f'`{MNGR_MERGE_KEY} = {{"{bare_dotted}" = "{op}"}}` (vanilla Claude ignores `__mngr_merge`).'
            )
        if key == MNGR_MERGE_KEY:
            raise ConfigParseError(
                f"`{rel_dotted}` in {location}: `{MNGR_MERGE_KEY}` is only allowed at the root of "
                f"`settings_overrides`, not nested. Use dotted key paths in the single root "
                f"`{MNGR_MERGE_KEY}` map to target nested keys."
            )
        _reject_internal_keys(value, rel + (key,), location)


def _record_mark(marks: dict[tuple[str, ...], str], at: tuple[str, ...], wanted: str, location: str) -> None:
    """Set ``marks[at] = wanted``, raising if it already holds a conflicting suffix."""
    existing = marks.get(at)
    if existing is not None and existing != wanted:
        raise ConfigParseError(
            f"`{MNGR_MERGE_KEY}` in {location} is inconsistent at `{'.'.join(at)}`: it is implied to "
            f"`{_suffix_op_word(existing)}` and `{_suffix_op_word(wanted)}` at once. A key "
            f"cannot be assigned and also have a descendant extended."
        )
    marks[at] = wanted


def _mark_path(marks: dict[tuple[str, ...], str], segments: tuple[str, ...], suffix: str, location: str) -> None:
    """Record that the key at ``segments`` takes ``suffix``, with every ancestor marked
    ``__extend`` so the recursive merge reaches the leaf.

    Raises if a path is marked with two conflicting suffixes -- e.g. a directive assigning
    ``permissions`` while another extends ``permissions.allow`` (the first wants the
    ``permissions`` dict replaced, the second needs it merged).
    """
    for index in range(1, len(segments)):
        _record_mark(marks, segments[:index], EXTEND_SUFFIX, location)
    _record_mark(marks, segments, suffix, location)


def _apply_marks(clean: dict[str, Any], marks: dict[tuple[str, ...], str], rel: tuple[str, ...]) -> dict[str, Any]:
    """Rewrite ``clean`` so each key carries the leaf suffix recorded for it in ``marks``."""
    result: dict[str, Any] = {}
    for key, value in clean.items():
        here = rel + (key,)
        new_key = key + marks.get(here, "")
        result[new_key] = _apply_marks(value, marks, here) if isinstance(value, dict) else value
    return result


def _walk(data: Any, segments: tuple[str, ...]) -> Any:
    """Walk ``data`` along ``segments`` through dicts; return ``_MISSING`` if any step is absent."""
    current: Any = data
    for segment in segments:
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def desugar_settings_overrides(override: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """Translate a ``settings_overrides`` patch from its ``__mngr_merge`` surface to the
    internal suffix form, so the rest of the merge pipeline is unchanged.

    Rejects raw operator suffixes (they belong in ``__mngr_merge``), then for each
    ``"dotted.path" -> op`` directive marks the targeted leaf key with the op's suffix and
    every ancestor with ``__extend``. A key absent from the map stays bare (a
    narrowing-checked assign). Each directive path must reference an existing key in the patch
    -- a dangling directive is a user error worth surfacing, not a silent no-op. ``path`` is
    the settings_overrides root (``agent_types.<name>.settings_overrides``), used to name the
    owning agent type in error messages.
    """
    location = f"the `{path[1]}` agent type's `settings_overrides`"
    directives = override.get(MNGR_MERGE_KEY)
    clean = {key: value for key, value in override.items() if key != MNGR_MERGE_KEY}
    _reject_internal_keys(clean, (), location)
    if directives is None:
        return clean
    if not isinstance(directives, Mapping):
        raise ConfigParseError(
            f'`{MNGR_MERGE_KEY}` in {location} must be a map of "dotted.key" -> '
            f'"extend"|"assign", got {type(directives).__name__}.'
        )
    marks: dict[tuple[str, ...], str] = {}
    for dotted, op in directives.items():
        if op not in OP_SUFFIXES:
            valid = " | ".join(sorted(OP_SUFFIXES))
            raise ConfigParseError(
                f'`{MNGR_MERGE_KEY}` in {location}: entry "{dotted}" = "{op}" is not a valid '
                f"operator (expected {valid})."
            )
        segments = tuple(dotted.split("."))
        if "" in segments:
            raise ConfigParseError(f'`{MNGR_MERGE_KEY}` in {location} has a malformed key path "{dotted}".')
        if _walk(clean, segments) is _MISSING:
            raise ConfigParseError(
                f'`{MNGR_MERGE_KEY}` in {location} targets "{dotted}", but that key is not set there. '
                f"Set the value under `settings_overrides` (the `__mngr_merge` map only annotates how "
                f"existing keys merge)."
            )
        _mark_path(marks, segments, OP_SUFFIXES[op], location)
    return _apply_marks(clean, marks, ())


# =============================================================================
# Provision-time fold + the __mngr_merge narrowing remediation
# =============================================================================


def _remediation_directives(base: Any, override: Any, prefix: str) -> dict[str, str]:
    """Return the ``__mngr_merge`` directives that resolve the narrowing at ``prefix``.

    Mirrors ``narrowing.narrowing_paths`` but yields a per-path operator and -- crucially --
    recurses *past* the dict-level short-circuit, so a dict that drops a sibling key still has
    its nested narrowings surfaced. The operator preserves the user's intent as closely as
    possible: ``extend`` for a dict that drops keys (so the unmentioned base siblings survive)
    and ``assign`` for a replaced list/set/whole value (so the user's exact value is kept, not
    silently broadened by base entries). A key containing a literal dot cannot be targeted by a
    dotted ``__mngr_merge`` path, so it is skipped rather than mis-advised.
    """
    if not isinstance(base, (list, tuple, dict, set, frozenset)) or not base:
        return {}
    if is_static_marker(override):
        return {}
    if isinstance(base, (list, tuple)):
        if isinstance(override, (list, tuple)) and all(entry in override for entry in base):
            return {}
        return {prefix: "assign"}
    if isinstance(base, (set, frozenset)):
        if isinstance(override, (set, frozenset, list, tuple)) and set(base) <= set(override):
            return {}
        return {prefix: "assign"}
    if not isinstance(override, dict):
        return {prefix: "assign"}
    result: dict[str, str] = {}
    if any(key not in override for key in base):
        result[prefix] = "extend"
    for key, sub_base in base.items():
        if "." in key or key not in override:
            continue
        result.update(_remediation_directives(sub_base, override[key], f"{prefix}.{key}"))
    return result


def _mngr_merge_remediation(base: Mapping[str, Any], override: Mapping[str, Any], narrowings: list[str]) -> str:
    """Render the ``__mngr_merge`` remediation: the exact patch that resolves every narrowing."""
    directives: dict[str, str] = {}
    for path in narrowings:
        segments = tuple(path.split("."))
        base_value = _walk(base, segments)
        override_value = _walk(override, segments)
        # A key containing a literal dot cannot be targeted by a dotted path; skip it.
        if base_value is _MISSING or override_value is _MISSING:
            continue
        directives.update(_remediation_directives(base_value, override_value, path))
    if not directives:
        return (
            "Declare the affected keys in a top-level `__mngr_merge` map in the agent type's "
            "`settings_overrides` -- `extend` to merge onto the base, `assign` to replace -- "
            'e.g. `__mngr_merge = {"permissions.allow" = "extend"}` (vanilla Claude ignores it).'
        )
    body = ", ".join(f'"{path}" = "{op}"' for path, op in directives.items())
    return (
        "To resolve this while keeping the base's other entries, add this to the agent type's "
        "`settings_overrides` (vanilla Claude ignores `__mngr_merge`):\n"
        f"    {MNGR_MERGE_KEY} = {{{body}}}\n"
        "`extend` merges onto the base; switch a key to `assign` to replace it instead."
    )


def apply_settings_patch(
    base_data: Mapping[str, Any],
    settings_overrides: Mapping[str, Any],
    *,
    allow_narrowing: bool,
    base_description: str,
) -> dict[str, Any]:
    """Fold a desugared ``settings_overrides`` patch onto a concrete base settings dict.

    Shared by the external-tool plugins (``mngr_claude``, ``mngr_antigravity``): both fold the
    agent type's ``settings_overrides`` onto a base (the user's synced home settings + the
    plugin's own additions) with identical semantics.

    Drops a top-level ``__mngr_merge`` key from the base (a no-op on the floor -- it merges
    onto nothing -- and the external tool ignores it, so it must not leak into the written
    file), normalizes the base to a concrete patch, then folds the override on top via the
    overlay typed-node algebra: a desugared ``key__extend`` merges onto the base value (list
    concat / set union / recursive dict merge), a bare ``Default`` key assigns, and ``Assign``
    / ``Static*`` suppress the narrowing otherwise recorded for a dropped aggregate. The
    override arrives already desugared from its ``__mngr_merge`` surface (``key_resolver``
    runs ``desugar_settings_overrides`` at config-load). ``finalize`` is total, so no marker
    survives into the result.

    Hard-errors (with the exact ``__mngr_merge`` remediation) when a bare override key at any
    depth drops a non-empty aggregate from the base, unless ``allow_narrowing`` is set.
    ``base_description`` names the side whose value would be dropped (e.g. the home
    ``settings.json`` path).
    """
    stripped_base = {key: value for key, value in base_data.items() if key != MNGR_MERGE_KEY}
    base = finalize(lift(stripped_base))
    merged, narrowings = merge_narrowing_allowed(lift(base), lift(dict(settings_overrides)))
    if narrowings and not allow_narrowing:
        detail_lines: list[str] = []
        for path in narrowings:
            detail_lines.append(f"  {path}")
            detail_lines.append("      assigned by the agent type's `settings_overrides`")
            detail_lines.append(f"      would drop a value from {base_description}")
        remediation = _mngr_merge_remediation(base, dict(settings_overrides), narrowings)
        raise ConfigParseError(build_settings_narrowing_message(detail_lines, remediation=remediation))
    return finalize(merged)
