The rest of the app's own copy now says Imbue Studio, and the noun for a workspace is consistent with the UI.

`minds run --help` now names the product ("Host/Port to bind the Imbue Studio bare-origin server to", "Do not open the Imbue Studio UI in the system browser"). The create form's web-access and backup-password helpers, the missing-restic error, the backup validation errors, and the cloud-accounts failure message all name Imbue Studio, as do about fourteen field descriptions in the OpenAPI document served at `GET /api/schema`. The `restic.env` written into every backed-up workspace is now headed "# Managed by Imbue Studio."

The wordmark announced itself to screen readers as "mind" -- in the shell's `alt` text and inside both copies of `mind-wordmark.svg` as the SVG's own `<title>`. All three now say Imbue Studio. The artwork and the asset filenames are untouched.

Where the old name meant the *workspace* rather than the agent, the word is now "machine", matching the Start/Stop vocabulary the landing page already shows: "Shutdown-capable machines" in the overview, and the shutdown-prompt and liveness code that produces it. The user's own computer stays "computer", so the two senses no longer share a word.

Two strings the product-name pass took too far are corrected to the agent noun: it is your agent, not the application, that asks you to connect an account, and that helps with the information in a service you connect.

The hello-world example's page -- the one you open in a browser to check forwarding works -- says "Hello World Agent". Doc page titles name the product ("Imbue Studio testing overview", "Running Imbue Studio on a Raspberry Pi", "Running Imbue Studio under WSL2"), the workspace glossary defines a template as a snapshot of what an agent built that another agent can be created from, and the latchkey doc's quoted sync copy again matches the shipped dialog word for word.
