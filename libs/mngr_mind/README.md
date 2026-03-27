# mngr_mind

Common code for mind-based agents in mngr. This plugin provides the shared infrastructure used by concrete mind plugins (like `mngr_claude_mind`).

## What this plugin provides

- **Event watcher**: Streams events and delivers them to the primary agent via `mngr message` with debouncing and rate limiting
- **Common data types**: Event types, watcher settings, and other shared types used across mind plugins
- **Provisioning**: Functions for provisioning the `link_skills.sh` script (symlinks shared top-level skills into role directories)

## What this plugin does NOT provide

This is not a standalone agent plugin. It does not register an agent type. It must be imported and used by concrete mind plugins that define their own agent types (e.g., `mngr_claude_mind` registers the `claude-mind` agent type).

## Dependencies

- `mngr` - the core agent management framework
- `mngr-llm` - LLM tool integration (conversations, provisioning utilities)
- `mngr-recursive` - watcher infrastructure (shared utilities for event/file watchers)
