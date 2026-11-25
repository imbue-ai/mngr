Imbue Studio no longer signs itself out after 30 days of running.

Before, the app got one session cookie when it started, and the backend stopped accepting that cookie 30 days later. A desktop app left running that long showed "Reconnecting…" indefinitely, and reopening a window landed on a sign-in page that only a full quit and relaunch could get past.

- The desktop app no longer signs in with the one-time login code. The backend it starts hands it a value that the app sets as each window's session cookie; the backend accepts exactly that value for as long as it runs, so the app's session cannot lapse. A backend started by the app mints no one-time code at all.

- "Restart Imbue Studio" on the sign-in page now signs the app back in. It restarted the backend without giving the windows a session the new backend accepts, and returned to the same page.

- In a plain browser, the backend now answers every request that presents a valid signed session cookie with a freshly signed one, so a session lasts for as long as it is used at least once every 30 days. An expired, altered or foreign cookie is never renewed.

- A backend started by the app logs the address it serves the app at, where it logged the one-time login URL. The launch-to-message harness (`scripts/launch_to_msg_e2e.py`) finds the backend from that line.

- The Electron end-to-end harness (`e2e_workspace_runner.py`) relaunches a launch whose backend says it could not start its `mngr latchkey forward`. It relaunched such a launch before only because no backend page appeared in time, and the app's windows now reach the backend sooner.
