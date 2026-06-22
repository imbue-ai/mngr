Fix `just minds-build` failing during `uv lock` with "no version of overlay==0.1.0".

`imbue-mngr` now depends on the unpublished workspace package `overlay`, but it was missing from the build's hand-maintained bundled-package lists, so no wheel was built for it and uv fell back to (nonexistent) PyPI. Added `overlay` to all four mirrored lists (`scripts/build.js`, `electron/env-setup.js`, `scripts/build_test.py`, and `electron/pyproject/pyproject.toml`) so it is bundled as a wheel and resolved locally.
