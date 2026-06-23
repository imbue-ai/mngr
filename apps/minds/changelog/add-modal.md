Added two "Modal (1-day ephemeral)" compute providers to the Create workspace form, alongside Docker / Lima / Vultr / AWS / Imbue Cloud. Modal sandboxes are ephemeral (~1 day), so both are testing-only:

- **Modal (1-day ephemeral) - Direct** runs the agent in a Modal sandbox created from this machine using your own Modal token. Selecting it expands an inline note with the auth steps (`uv tool install modal`, then `modal token new`, or `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`). Backed by the `[providers.modal]` block (DIRECT mode), written at startup.

- **Modal (1-day ephemeral) - Proxied** is keyless (no Modal token on your machine) -- it routes sandbox creation through the imbue_cloud connector. Selecting it shows a note that it is not ready yet (it needs the connector deployed with the sandbox-broker routes). Backed by the `[providers.modal_proxied]` block (PROXIED mode, pointing at the same connector URL as imbue_cloud), registered when a gateway session exists.

Both options surface their per-mode helper text in the create form the same way the AWS option shows its credentials note, and appear in the workspace listing as "Modal (Direct)" / "Modal (Proxied)".
