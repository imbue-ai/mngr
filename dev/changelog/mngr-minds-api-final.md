Updates `blueprint/minds-workspace-api/HANDOFF.md` for the SSH work: marks SSH access between workspaces (item #5) as done -- grant pruning plus refresh-not-stack are wired in (in `apps/minds`), and the remote->local SSH tunnel broker is now implemented. Corrects the earlier (incorrect) "blocker" note: a local Docker/Lima target uses an SSH connector, so the hub *can* resolve its `127.0.0.1:<published port>` endpoint and brokers a reverse tunnel into the calling workspace.

Adds `openapi-spec-validator` to the root dev dependency group; it validates the minds `GET /api/schema` OpenAPI document in tests (test-only -- not shipped in the minds wheel).

Adds `blueprint/minds-api-spectree/plan-minds-api-spectree.md`: the design + implementation-status doc for converting the minds `/api/v1` to spectree + pydantic validation (records what landed, the spectree-2.0.1 behaviors that shaped the approach, and the resolved design decisions).
