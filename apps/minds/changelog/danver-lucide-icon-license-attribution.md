# Attribute the lucide icon set the UI is drawn from

Every glyph the desktop client draws through `Icon16` -- titlebar and sidebar
marks, workspace-row actions, form chevrons, status badges -- is
[lucide](https://lucide.dev) artwork, but nothing shipped said so. Fourteen of
them are lucide's path data unchanged; the rest were traced from the lucide
glyph of the same name in Figma. Lucide is licensed under ISC, which requires
its copyright and permission notice to appear in every copy, and part of the
set is additionally covered by Feather's MIT license.

Both notices now ship, by the two routes a copy can travel:

- `imbue/minds/desktop_client/static/ATTRIBUTION.md` records where the artwork
  came from and reproduces lucide's `LICENSE` in full. It sits inside the
  package, so it is in the wheel beside the bundle it credits, next to the
  existing `service_icons/ATTRIBUTION.md` for the service brand marks.

- A `/*! ... @license ... */` banner at the top of the icon source carries the
  same notice into the minified SPA bundle itself, which is what reaches a
  user who has only the built assets.

No icon changed.
