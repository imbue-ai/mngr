Drop latchkey's built-in `notion` service from the bundled `services.json` catalog.

`notion` was already hidden from agents via `settings.hideBuiltinServices`, but it still shipped in the permission catalog, so it kept showing up in catalog-driven surfaces (the connectors page, the permission dialog, the onboarding carousel) as a service whose credentials latchkey never injects. It is now absent from the catalog, leaving only the separate `notion-mcp` integration.

`scripts/generate_services_json.py` skips every service in `core.HIDDEN_BUILTIN_SERVICES` when regenerating the catalog from detent's schemas, so the two cannot drift apart.
