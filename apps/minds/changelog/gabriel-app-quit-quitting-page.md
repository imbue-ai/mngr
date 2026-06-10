# Quitting page on app quit

- When a quit is committed, every open window now flips to a full-window "quitting" screen -- the same animated wordmark as the startup loading screen, with a status line -- and stays on it until the app closes. This replaces the previously frozen-looking UI during backend teardown.
- The native prompt asking whether to shut down still-running local minds still runs first and is unchanged; only after you commit (Leave running / Shut down) do the windows flip. Cancelling that prompt leaves the app fully intact with no visual change.
- When you choose "Shut down", the stop progress ("Stopping N minds…") now shows in-page on the quitting screen. The small frameless "Stopping minds…" window has been removed. If some minds can't be stopped, the native Retry / Quit anyway / Cancel quit dialog still appears; "Cancel quit" reverses the flip and returns the app to its normal running state.
- All open windows show the quitting page. Headless quits (`just minds-stop` / SIGTERM) tear down without any interactive UI, as before.
