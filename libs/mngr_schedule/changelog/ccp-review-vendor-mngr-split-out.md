`get_mngr_dockerfile_path` now resolves the mngr Dockerfile from the `imbue.mngr` package location (via `importlib.resources`) for every install mode, instead of navigating from the enclosing git root in EDITABLE/SKIP mode.

The Dockerfile is a packaged resource at `imbue/mngr/resources/Dockerfile`, so the package-derived path is identical in a standalone checkout and also correct when the monorepo is vendored inside another git repository (where the enclosing git root is not the mngr checkout).
