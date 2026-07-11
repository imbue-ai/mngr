from pathlib import Path

# A file shipped with mngr lives in one of two places depending on how mngr was installed.
# In a wheel it is force-included under the package at ``imbue/mngr/<relative_path>``
# (``_PACKAGE_ROOT``, this file's ``parents[1]``). In a source/editable checkout the same
# file lives at ``libs/mngr/<relative_path>`` (``_SOURCE_ROOT``, ``parents[3]``) because the
# top-level ``libs/mngr`` tree is not otherwise packaged (see CLAUDE.md).
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = Path(__file__).resolve().parents[3]


def resolve_shipped_path(relative_path: str) -> Path | None:
    """Resolve a file shipped with mngr, preferring the packaged copy over the source tree.

    Returns None when the file exists in neither location (e.g. a build artifact that a
    source checkout has not generated yet), so callers can degrade gracefully.
    """
    packaged_path = _PACKAGE_ROOT / relative_path
    if packaged_path.exists():
        return packaged_path
    source_path = _SOURCE_ROOT / relative_path
    if source_path.exists():
        return source_path
    return None
