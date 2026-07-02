Extracted the desktop client's trusted app shell (the fixed titlebar, the browser-mode floating sidebar, and the accent / `chrome.js` wiring) into a reusable `ChromeShell.jinja` layout component.

`Chrome.jinja` is now a thin wrapper that fills the `ChromeShell` content slot with the browser-mode content iframe; its rendered output is unchanged. This is a preparatory, no-op refactor toward serving trusted local pages (Landing, Create, Settings, ...) with the titlebar directly on the chrome surface, so the content surface can eventually host agent content only.
