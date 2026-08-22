"""Pinned pip-set exports for our Modal service images.

Each Modal service app declares its exact image pip set (==-pinned) in a
``[dependency-groups] image`` in its own pyproject.toml; the group resolves
inside the workspace ``uv.lock``, and the canonical ``uv export`` command
below renders it (with transitive pins and sha256 hashes) to a committed
``image_requirements.txt`` next to the app, which is what the image installs.
Regenerate the committed exports with ``just export-image-requirements``.

This module holds only the pure pieces (constants, paths, the command),
kept in the public ``imbue_common`` even though its remaining consumers
(``minds-admin env deploy``'s freshness preflight and the private Modal
apps' drift tests) are private, so the mirror's ``imbue_common`` stays
self-contained without the private ``modal_app_kit`` package, which holds
the modal-SDK side (the digest-pinned base and ``pinned_image``).
"""

from pathlib import Path
from typing import Final

# The pyproject dependency group holding each app's ==-pinned image pip set.
IMAGE_DEPENDENCY_GROUP: Final[str] = "image"

# Keep in sync with the container-safe copy in imbue.modal_app_kit.image
# (which the Modal entrypoints import instead, since shipped modal_app_kit
# code may not depend on imbue_common); a modal_app_kit test asserts equality.
IMAGE_REQUIREMENTS_FILENAME: Final[str] = "image_requirements.txt"

# The uv workspace package names (pyproject [project] name) of the apps whose
# images install from a committed, hash-locked export.
IMAGE_PINNED_PACKAGE_NAMES: Final[tuple[str, ...]] = (
    "remote-service-connector",
    "modal-litellm",
    "oauth-redirector",
    "analytics",
)


class ImageRequirementsExportError(Exception):
    """Raised when rendering an image requirements export from uv.lock fails."""


def image_pinned_app_dir(package_name: str) -> str:
    """The app's directory (repo-root-relative) holding its committed export."""
    return f"apps/{package_name.replace('-', '_')}"


def image_requirements_path(repo_root: Path, package_name: str) -> Path:
    return repo_root / image_pinned_app_dir(package_name) / IMAGE_REQUIREMENTS_FILENAME


def image_requirements_export_command(package_name: str) -> list[str]:
    """The canonical uv invocation rendering an app's image pip set from uv.lock.

    Offline and frozen: the output is a pure function of the committed lock,
    so the justfile recipe, the per-app drift tests, and the deploy preflight
    all replay this exact command and byte-compare. ``--no-header`` keeps the
    invocation line out of the output so the comparison is stable.
    """
    return [
        "uv",
        "export",
        "--frozen",
        "--offline",
        "--quiet",
        "--no-header",
        "--format",
        "requirements-txt",
        "--package",
        package_name,
        "--only-group",
        IMAGE_DEPENDENCY_GROUP,
    ]
