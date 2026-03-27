# LLM FAQ

Frequently asked questions for LLMs working with the mngr codebase.

## Can multiple users share SSH access to a host?

Technically you could share the corresponding private key (bad idea) or add someone else's authorized key via a provisioning step, but this is not a supported workflow. It would result in weird "spooky action at a distance" from someone else mucking about in your container. mngr is designed for single-user operation where your hosts are YOUR hosts.

## Why doesn't mngr support multi-user/team features?

mngr is philosophically aligned with the idea that your agents are YOUR agents—fully aligned to and controlled by you. All interactions with mngr are considered fully private. If you want to expose agent data to others, you can build that yourself via plugins or external tools, but it's entirely voluntary and under your control.

## Why is TYPE_CHECKING used in interfaces/host.py and interfaces/agent.py?

The style guide generally discourages using the `TYPE_CHECKING` guard because it's often a sign of poor architecture. However, there are **two exceptions** in this codebase:

1. `interfaces/host.py` - imports `ProviderInstanceInterface` under TYPE_CHECKING
2. `interfaces/agent.py` - imports `HostInterface` under TYPE_CHECKING

These exceptions exist to make the API more convenient for end users who want to traverse parent-child relationships (e.g., `agent.get_host()` or `host.get_provider_instance()`). Without TYPE_CHECKING, we would have circular imports. The alternative would be to use string annotations everywhere, which is less ergonomic.

This is an intentional trade-off: slightly messier imports in exchange for a better user experience.

YOU SHOULD NEVER ADD NEW USES OF TYPE_CHECKING (without explicit approval from the user.)

## Why do some methods return dict instead of structured types?

The style guide says "Never use `dict` unless the keys are truly dynamic." However, some methods in mngr intentionally return `dict[str, str]`:

- `Host.get_env_vars()` - environment variables are dynamic key-value pairs
- `Host.get_tags()` - tags are dynamic metadata
- `Host.get_plugin_data()` - plugin data schemas vary by plugin

These are **intentional exceptions** because the keys are genuinely dynamic and user-defined. Creating fixed types for these would be inappropriate.

## Should the commands be interactive?

Most commands should be pure CLI, with the following exceptions (which have minor TUI components):
- `mngr create` (so that you can enqueue a message)
- `mngr pair` (so that you can select how to disconnect, pause, etc.)
- `mngr cleanup` (has interactive mode to make it easier to figure out what you want to nuke)
- `mngr connect` (if you don't specify an agent, can give you a little TUI for selecting it)
