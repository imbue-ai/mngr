# Future Work

Features and improvements planned for future versions.

## Plugin Discovery

Currently, users must know plugin names to install them. A `mngr plugin search` command could:
- Search a plugin registry for available plugins
- Show what agent types and providers each plugin provides
- Display ratings, download counts, etc.

## Multi-Agent Communication

Currently, there's no direct affordance for agent-to-agent communication beyond `mngr message`. Future options:
- Plugins that expose state to a shared database or message queue
- Port forwarding to expose services between agents
- Setting up pairing between multiple agents for shared file access

## Cost Tracking

For cloud providers like Modal:
- Track spend per agent
- Visibility into resource consumption per agent
- Spend limits (currently must be set via provider directly)

Eventually plugins could provide cost tracking dashboards.

## Submodule Support

Git submodules are not currently supported. Future work could:
- Handle recursive `.git` directories properly
- Sync submodule state alongside main repo

## Scoped-Down Credentials for Child Agents

See [specs/plugins/recursive_mngr.md](./plugins/recursive_mngr.md) for details on the security model for recursive agent creation.
