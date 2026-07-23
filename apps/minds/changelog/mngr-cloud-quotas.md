Per-account plans and quotas (enforced by the remote service connector) surface in minds:

- The Manage Accounts page shows each signed-in account's plan, quota limits, and live usage, with a plan selector ("explorer" / "ally"; switching to an ineligible plan shows the server's reason).

- The "remote" create preset now defaults the AI provider to SUBSCRIPTION: imbue-cloud inference keys are quota-gated per plan (the explorer plan has a $0 LLM budget), so the user's own Claude subscription is the default that works for everyone.

- Backup provisioning reuses an existing bucket by rolling its single key (`bucket roll-key`) instead of minting extra keys.

- `minds env deploy` writes the committed `[plans]` blocks from each tier's deploy.toml into the connector's plans table after migrations (git-owned plan definitions; per-user rows are managed via the admin API).

- New deployment tests cover explorer-plan behavior end to end: fresh accounts land on explorer, $0-budget key minting is refused, ally requires partner access, and under-quota leases reach the pool.
