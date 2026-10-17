Apply pool-hosts schema migrations over Neon's direct (non-pooled) host.

The `host_pool` migration runner (`envs/migrations.py`) now strips Neon's `-pooler` suffix from the DSN hostname before applying schema migrations (and their bookkeeping in `schema_migrations`): migrations can rely on session-scoped behavior (advisory locks, `CREATE INDEX CONCURRENTLY`) that is unsafe through PgBouncer's transaction-mode pooling. The resolved host is logged at deploy time for verification.

Plain DML seeding (plan definitions, paid-list defaults) intentionally stays on the pooled DSN.
