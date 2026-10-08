The migration failure dialog now names the product: its title and body read
"Imbue Studio could not move its data" / "Imbue Studio could not finish moving
its data out of ...", where both said "Minds". Both now read the name from
`PRODUCT_DISPLAY_NAME` rather than repeating the literal, so neither can drift
from the app's own name again. The same text is what gets written to
`migration-failure.log`.

The SPA dev-preview page and the visual-diff harness's rendered index page both
had stale document titles ("minds frontend dev preview", "Mind"); the harness
now matches the title the real page serves. The build script's console output,
the uv-shim comment in `scripts/download-binaries.js`, the launch-to-msg
screenshot comment, and the Desktops section of `docs/latchkey-permissions.md`
say Imbue Studio too.

`electron/product-name.js` carried a comment claiming `package.json`'s
`productName` "carries no space" and was distinct from the display name; it has
held `Imbue Studio` since 0.8.0, identical to the display name, so the comment
now states what the two are for instead.
