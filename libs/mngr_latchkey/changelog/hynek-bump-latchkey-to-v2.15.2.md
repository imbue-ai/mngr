Regenerated the latchkey `services.json` permission catalog from detent 1.5.0.
This adds the new `notion-mcp` service (Notion's hosted MCP endpoint at
`mcp.notion.com`, scope `notion-mcp-api`, displayed as "Notion (MCP)") with its
20 grantable permissions, and refreshes the Slack `slack-read-all` /
`slack-write-all` descriptions to match detent's updated wording. The catalog
generator (`scripts/generate_services_json.py`) gained curated display-name and
service-order entries for `notion-mcp`.
