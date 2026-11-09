Imbue Studio 0.10.1: the desktop app's version moves to 0.10.1 and its pinned workspace template to `minds-v0.10.1`, a patch release carrying everything on `main` since 0.10.0, so workspaces already on 0.10.0 are not prompted to update.

The build's ToDesktop upload estimator now understands the root-level file exclusions `todesktop.js` declares (`!.env`, `!.env.*`) alongside `!<dir>/**`, with fast-glob's top-level-only matching, so `pnpm dist` builds again; it still refuses any appFiles shape it has not been taught, and a unit test holds it to the real `todesktop.js`.
