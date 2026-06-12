The `claude_subagent_proxy` plugin is now **disabled by default** and must be explicitly opted into. It only loads when a config layer sets:

```toml
[plugins.claude_subagent_proxy]
enabled = true
```

This inverts the usual plugin default (load-unless-disabled) because the plugin is very experimental and interferes with a lot of other tooling -- it intercepts Claude Code's built-in `Task` tool. The README documents the new opt-in requirement and behavior.
