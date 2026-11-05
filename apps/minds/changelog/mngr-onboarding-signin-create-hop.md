After a browser sign-in, the first-run Imbue Cloud create no longer waits on a second email-verification check or on the local Docker probes before it goes out.

- `GET /accounts/verification` answers verified at once when the latest browser sign-in for the address in this app run (within the last hour) reported it verified, instead of starting a `mngr imbue_cloud auth is-verified` process. Every other case still asks the connector.

- `GET /ui/api/create/form-defaults` takes a new `?skip_local_probes=1`, which leaves out the local Docker probes; the start flow uses it.

- While a cloud create is still waiting, the start page shows a spinner with "Checking your account…" during the verification check, then "Starting your workspace…" while the create is sent. An unverified email gets the verification question instead.

- A cloud create the connector refuses for an unverified email fails on the creating page with the connector's own sentence instead of the `mngr create` transcript, and is not reported as an error, like a waitlist refusal.
