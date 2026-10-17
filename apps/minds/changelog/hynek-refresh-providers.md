Fixed remote workspaces of an account signed in after the app started being unreachable from the Permissions tab. Approving a connection for such a workspace (or reading its connections, or changing a toggle) failed with `Could not reach the machine of host <id>: Unknown provider backend: imbue_cloud_<account>`.

Signing in registers a provider instance for the account in mngr's settings, but the app loaded its provider set once at startup and kept it, so a set taken before the sign-in knew that provider only as a name. Creating a workspace was unaffected, because that runs `mngr` in a fresh process -- only the operations the app performs in-process failed, and they kept failing until the app was restarted.

The app now reloads its provider set whenever mngr's settings file changes, which also picks up an account signed out, a cloud account added or removed, and an edit made to the settings by hand while the app runs. A reloaded-away provider set is retired in full -- every instance in it closed and dropped, and its suspension watchdog stopped -- so a long session of sign-ins and provider toggles does not accumulate either.

Settings that will not load -- a hand edit with a typo, a provider block naming a backend this install has not got -- leave the provider set the app already holds in place rather than failing every Permissions operation, and the app picks the settings up as soon as they parse again.

Fixed `test_broadcast_after_connect_reaches_the_ws_client`, which failed once under a parallel run of the whole desktop-client suite. It drained the connect sequence by counting frames, and the count it used need not match what the connection received; it now waits for its connection to be registered and reads until the pushed frame arrives.
