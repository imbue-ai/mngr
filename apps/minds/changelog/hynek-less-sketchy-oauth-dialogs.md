- Signing in to any MCP-based service (the builtin `*-mcp` connectors, and a custom service that signs in through an MCP server's OAuth flow) now returns through the callback page Imbue Studio hosts, as Notion MCP's sign-in already did, so the provider's consent screen no longer shows a localhost redirect URL.

- The bundled latchkey moves to 3.24.0, which names the OAuth client it registers for an MCP sign-in after the app ("Imbue") alone, the name the consent screen shows.
