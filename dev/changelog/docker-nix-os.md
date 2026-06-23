Added an `AGENTS.md` compatibility shim so Codex can discover and follow the repository's canonical `CLAUDE.md` instructions.

Added an explicit `just minds-build-fct-nixos` gate that runs the FCT Docker/NixOS profile build to verify the checked-in Nix closure manifest, then runs the heavyweight Docker image contract test against `Dockerfile.nixos`.
