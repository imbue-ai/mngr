Default `PLAYWRIGHT_BROWSERS_PATH` to `/opt/ms-playwright` in the minds test
conftest when that directory holds an install and the variable isn't already
set. This keeps the shared autouse HOME isolation from hiding a chromium
install (matching the location the snapshot image bakes in), so the
playwright-driven creating-page redirect test can find the browser.
