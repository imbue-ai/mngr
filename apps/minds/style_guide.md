# Style guide deltas for minds

All rules from the [normal style guide](../../style_guide.md) apply to minds, with the following exceptions and additions:

1. async code is permitted sparingly (since we use FastAPI), but wherever possible, we should still prefer to use synchronous routes. In practice this means that things like web socket related code and routes may end up being async, but most of the rest should stay sync.

# Product name

The product is Imbue Studio (previously Mind, and Minds before that).

- Write "Imbue Studio" in full at the first mention on a surface (a page, a dialog, a skill, a README, a help text).
- Write "Studio" for later mentions on the same surface.
- Never put the product name into an identifier: no code names, env vars, paths, package names, or tags. Internal identifiers keep their `minds` spelling (`MINDS_*`, `~/.minds/`, the `minds` CLI, the npm package name, `minds-v*` tags).
- In Electron code, read the display name from `PRODUCT_DISPLAY_NAME` in `electron/product-name.js` rather than repeating the literal; `package.json`'s `productName` is the file-system name (`Imbue Studio`, which the bundle and its helpers are named after); `PRODUCT_DISPLAY_NAME` is what the app writes in its own UI.

The rename PR changed only what the app identity forces (the OS-visible names, the deep link scheme, and the document titles). The sweep of the remaining "Mind" prose across the app copy, skills, onboarding, legal pages, and template documentation is a separate PR that applies this rule.
