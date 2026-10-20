Bump Latchkey to 3.17.0.

`Latchkey.auth_browser_login` now runs `latchkey auth browser <service> --strict`, so a browser sign-in fails with a message naming the missing permissions, instead of storing a partial credential, when the user grants fewer permissions than the service asked for (for example by leaving some Google consent checkboxes unticked). Only services that can tell what was granted enforce it; for the others the flag is a no-op.
