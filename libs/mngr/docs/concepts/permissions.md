# Permissions

Agents have a list of "permissions" that control both what they are allowed to do and what information they have access to [future].

## How Permissions Work

Permissions are **opaque strings**. mngr itself knows nothing about their content or format -- it simply stores them as a list and passes them along to plugins. All interpretation of what a permission means is done by plugins.

This means:

- **Any string is a valid permission.** You can use simple labels (`network`, `internet`), namespaced strings (`github:*`, `anthropic:claude-code:write`), or any other format -- as long as it's a non-empty string.
- **Revoking a permission requires an exact string match.** When you use `--revoke`, mngr compares strings exactly. If you granted `"github:read"`, you must revoke exactly `"github:read"` -- not `"github:READ"` or `"Github:read"`.
- **mngr does not validate permissions.** It will accept any string you grant. Whether a permission actually does anything depends on whether a plugin recognizes it.

A common convention is to namespace permissions by plugin name:

```
[
    "github:*",
    "anthropic:claude-code:write",
    "user_data:email",
    ...
]
```

where the first part is the plugin name (e.g., `github`, `anthropic`, `user_data`) and everything after is plugin-specific. But this is only a convention -- plugins define what strings they recognize.

## Permission Scope

All agents on the same host effectively share the **union** of all permissions on that host. If you need isolation between agents, use separate hosts. See [Security Model](../security_model.md) for details.

Changing permissions requires an agent restart to take effect.

## Available Permissions

Run [`limit --help`](../commands/secondary/limit.md) [future] for the full list of available permissions.
