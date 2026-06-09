Added `mngr list --schema`, a machine- and human-readable catalog of every field you can reference in `--include`/`--exclude`, `--sort`, and `--fields`/`--format`.

- `mngr list --schema` lists each referenceable field with its type, description, and the contexts it works in (filter / sort / template). It composes with `--format json`, `--format jsonl`, and `--format` template strings, and is rejected (with a clear error) if combined with any agent-selection option since the catalog is static.

- The catalog is derived live from the real data shape (`AgentDetails`/`HostDetails`), so it always reflects the actual models -- including deeply nested fields like `host.resource.cpu.count` and `host.ssh.host`. The non-model fields (the computed `age`/`runtime`/`idle`, the `host.provider`/`project` aliases, and dynamic patterns like `labels.$KEY`) are listed explicitly and pinned to the real computation/alias tables by tests.

- The `mngr list` help "Available Fields" section (and the generated `docs/commands/primary/list.md` on GitHub) is now rendered from this same catalog, so the documented fields can no longer drift from the models.
