"""Hatchling build hook: build the desktop-client SPA bundle into the wheel.

Runs ``pnpm install --frozen-lockfile && pnpm build`` in ``frontend/`` before a
*standard* wheel build so the wheel is self-contained (the bundle lands in
``imbue/minds/desktop_client/static/ui/``, force-included via the
``[tool.hatch.build] artifacts`` list). Editable builds (``uv sync``, offload
sandboxes) skip the hook entirely -- they run from the working tree, where the
dev loop's ``pnpm dev`` watch build owns the bundle, and must not require
node/pnpm.
"""

import os
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class FrontendBundleMissingError(RuntimeError):
    """Raised when the frontend build ran but left no bundle for the wheel.

    Local class (not the minds error hierarchy) because this hook runs inside
    hatchling's isolated build environment, where the ``imbue.minds`` package
    and its dependencies are not importable.
    """


class FrontendBuildHook(BuildHookInterface):
    """Builds frontend/ into static/ui/ for standard (non-editable) wheel builds."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        # Tooling that builds wheels only to inspect packaging metadata (the
        # offload-sandbox bundling-contract tests) sets this to skip the
        # node/pnpm requirement; real packaging never sets it, so a release
        # wheel cannot silently ship without its bundle.
        if os.environ.get("MINDS_SKIP_FRONTEND_BUNDLE") == "1":
            return
        frontend_dir = Path(self.root) / "frontend"
        subprocess.run(
            ["pnpm", "install", "--frozen-lockfile"],
            cwd=frontend_dir,
            check=True,
            timeout=600,
        )
        # src/generated/ui.ts is gitignored (regenerated from the pydantic
        # JSON Schema), so a fresh checkout must generate before tsc runs.
        subprocess.run(["pnpm", "generate"], cwd=frontend_dir, check=True, timeout=600)
        generated_types = frontend_dir / "src" / "generated" / "ui.ts"
        if not generated_types.exists():
            # generate-types.mjs exits 0 when the monorepo schema generator is
            # not reachable (e.g. building outside the checkout, such as from
            # an sdist); fail HERE with a clear message instead of letting
            # pnpm build die later on an opaque unresolved-module tsc error.
            raise FrontendBundleMissingError(
                f"pnpm generate completed without producing {generated_types}; the wheel must be "
                "built from the monorepo checkout so the schema generator is reachable"
            )
        subprocess.run(["pnpm", "build"], cwd=frontend_dir, check=True, timeout=600)
        bundle_manifest = Path(self.root) / "imbue/minds/desktop_client/static/ui/.vite/manifest.json"
        if not bundle_manifest.exists():
            raise FrontendBundleMissingError(
                f"Frontend build completed but {bundle_manifest} is missing; the wheel would not be self-contained"
            )
