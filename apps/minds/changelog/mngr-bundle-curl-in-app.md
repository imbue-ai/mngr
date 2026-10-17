The packaged app now ships the Chrome-impersonating curl it has always intended to use, so agent requests to services behind Cloudflare-style bot protection stop being blocked on TLS fingerprint.

`build.js` stages every binary the packaged app reads from `resources/`; the `todesktop:beforeInstall` hook also downloads binaries, but it runs against `app-wrapper/app/`, so its output lands in `app.asar` where nothing reads it. The datalib curl was fetched only by the hook, so no release ever carried a usable copy -- the app fell back to system curl, silently, since `latchkeyCurlEnv()` treats an absent dispatch curl as "no override" rather than an error. Dev machines were unaffected, because `ensure-binaries.js` populates `resources/` there. This is the same trap `desync` fell into, and the guard added for that case did not cover curl.

Only local (Lima) workspaces are affected. Their gateway runs on the desktop, so the outbound TLS handshake is made by the app's curl; remote workspaces run a gateway on the VPS and take their curl from `/usr/local/bin`.

`build.js`'s downloader guard gains `downloadLatchkeyCurl`, so a future refactor cannot drop it back out.
