"""Parsing and semver ordering for ``minds-v*`` default-workspace-template tags.

Grammar and ordering must match ``update_target.py``'s ``parse_version`` in the
default-workspace-template's update-self skill, which is what actually picks an update target.
"""

import re
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

# Mirrors update_target.py's ``_TAG_RE``.
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^minds-v(\d+)\.(\d+)\.(\d+)(?:-(?P<pre>.+))?$")


class MindsVersion(FrozenModel):
    """A ``minds-v*`` tag's version, ordered by ``<`` the way semver orders."""

    major: int = Field(description="Major version")
    minor: int = Field(description="Minor version")
    patch: int = Field(description="Patch version")
    release_rank: int = Field(description="0 for a prerelease, 1 for the release it precedes")
    prerelease: tuple[tuple[int, int, str], ...] = Field(
        description="Sort key of the prerelease identifiers; empty for a stable release"
    )

    def _sort_key(self) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
        """Element order here IS the precedence order."""
        return (self.major, self.minor, self.patch, self.release_rank, self.prerelease)

    def __lt__(self, other: "MindsVersion") -> bool:
        return self._sort_key() < other._sort_key()


def _prerelease_sort_key(pre: str) -> tuple[tuple[int, int, str], ...]:
    """Order a prerelease's dot-separated identifiers the way semver does.

    Numeric identifiers compare numerically and rank below alphanumeric ones;
    ``isdecimal`` (not ``isdigit``) matches semver's ``[0-9]+``.
    """
    identifiers: list[tuple[int, int, str]] = []
    for identifier in pre.split("."):
        if identifier.isdecimal():
            identifiers.append((0, int(identifier), ""))
        else:
            identifiers.append((1, 0, identifier))
    return tuple(identifiers)


def parse_minds_version(ref: str | None) -> MindsVersion | None:
    """Return the :class:`MindsVersion` of a ``minds-v*`` tag, or None for any other ref."""
    if ref is None:
        return None
    match = _TAG_RE.match(ref.strip())
    if match is None:
        return None
    pre = match.group("pre")
    return MindsVersion(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        release_rank=0 if pre is not None else 1,
        prerelease=_prerelease_sort_key(pre) if pre is not None else (),
    )
