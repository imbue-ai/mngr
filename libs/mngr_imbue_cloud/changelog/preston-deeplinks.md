`mngr imbue_cloud auth oauth` gained a `--success-redirect-url` option. When set, the localhost callback listener's "You are signed in" page immediately redirects the browser to that URL (with a visible fallback link) instead of telling the user to return to their terminal. The minds desktop app passes its `minds://` deeplink here so completing a browser OAuth sign-in hands focus straight back to the app.

The sign-in success page is also restyled: centered layout, system fonts, and light/dark color-scheme support instead of bare unstyled HTML.
