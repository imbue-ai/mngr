"""Typed operator nodes: the three operator wrappers and the ``Patch`` shape.

The typed-node model replaces operator *parsing* with operator *typing*: the
per-key precedence operator lives in the **type** of a dict-value node, not in a
key-string suffix. Three frozen node wrappers encode the three operators:

- ``Default(payload)`` -- assign, replacing the layer below; narrowing-checked
  (the bare ``key`` behavior).
- ``Assign(payload)`` -- assign without the narrowing check (``key__assign``).
- ``Extend(payload)`` -- merge onto the layer below (``key__extend``).

A ``Patch`` is a ``dict[str, Node]``. The load-bearing invariant is that a node's
``payload`` is **never** a bare ``Node``: it is a leaf (scalar/list/tuple/set/
frozenset, including the ``Static*`` leaf subclasses) or a nested ``Patch``. The
``lift`` pass (in ``node_merge``) establishes this invariant; the algebra preserves
it and only ever inspects/rewrites the *outermost* wrapper, never unwrapping a
payload to look for an inner operator. That is what makes stacked suffixes
(``a__extend__assign``) safe: they lift to a literal field name with a single
wrapper and are never re-parsed.

This module holds only the types (so the lift / merge algebra in ``node_merge`` can
import them without a cycle). Nodes are frozen dataclasses rather than pydantic
models because the library is dependency-free.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Default:
    """Assign node, replacing the layer below; narrowing-checked (bare ``key``).

    Carries a single ``payload`` (a leaf or a nested ``Patch``). Compared by
    type+payload; never hashed (payloads may be unhashable).
    """

    payload: Any


@dataclass(frozen=True)
class Assign:
    """Assign node, replacing the layer below **without** the narrowing check.

    The ``key__assign`` opt-out ("I am replacing this, don't warn"). Carries a
    single ``payload`` (a leaf or a nested ``Patch``).
    """

    payload: Any


@dataclass(frozen=True)
class Extend:
    """Extend node, merging onto the layer below (``key__extend``).

    List concat, set union, recursive patch merge. Never narrows (an extend is a
    superset). Carries a single ``payload`` (a leaf or a nested ``Patch``).
    """

    payload: Any


# A node is one of the three operator wrappers; a patch maps field names to nodes.
Node = Default | Assign | Extend
Patch = dict[str, Node]


def is_assign_kind(node: Node) -> bool:
    """Return True if ``node`` is an assign-kind node (``Default`` or ``Assign``).

    Assign-kind nodes replace the layer below wholesale; ``Extend`` does not.
    """
    return isinstance(node, (Default, Assign))
