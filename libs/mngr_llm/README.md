# mngr_llm

LLM agent plugin for mngr. Runs the `llm` CLI tool as an agent with conversation management, supporting services, and web interface.

## Overview

This plugin provides:

- **LLM agent type**: Registers the `llm` agent type that runs the `llm` CLI tool
- **Conversation management**: SQLite-based conversation storage with the `mind_conversations` table
- **Supporting services**: Chat scripts, conversation watcher, web server, and ttyd dispatch scripts
- **LLM tools**: Context gathering tools (`context_tool.py`, `extra_context_tool.py`) for providing agents with situational awareness
- **Settings**: TOML-based configuration via `minds.toml` (chat model, context settings, provisioning timeouts)

## Dependencies

- `mngr` - the core agent management framework
- `mngr-recursive` - host-level mngr provisioning
- `watchdog` - filesystem event monitoring for watchers
