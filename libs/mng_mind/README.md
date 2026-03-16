# mng_mind

Common code for mind-based agents in mng. This plugin provides the shared infrastructure used by concrete mind plugins (like `mng_claude_mind`).

## What this plugin provides

- **Event watcher**: Streams events and delivers them to the primary agent via `mng message` with debouncing and rate limiting
- **Common data types**: Event types, watcher settings, and other shared types used across mind plugins
- **Provisioning**: Functions for provisioning the `link_skills.sh` script (symlinks shared top-level skills into role directories)

## What this plugin does NOT provide

This is not a standalone agent plugin. It does not register an agent type. It must be imported and used by concrete mind plugins that define their own agent types (e.g., `mng_claude_mind` registers the `claude-mind` agent type).

## Dependencies

- `mng` - the core agent management framework
- `mng-llm` - LLM tool integration (conversations, provisioning utilities)
- `mng-recursive` - watcher infrastructure (shared utilities for event/file watchers)
