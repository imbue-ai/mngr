Added an `AGENTS.md` compatibility shim so Codex can discover and follow the repository's canonical `CLAUDE.md` instructions.

Added an explicit `just minds-build-fct-nixos` gate that runs the FCT Docker/NixOS profile build to verify the checked-in Nix closure manifest, then runs the heavyweight Docker image contract test against `nix/Dockerfile`.

Added a Docker/NixOS compute option to the Minds create-workspace UI and route it through the `docker-nixos` FCT template when launching local Docker workspaces.

Copied forward the NixOS Docker workspace design doc from the earlier mngr branch and updated it for the current FCT implementation, including the `nix/Dockerfile` path, stable flake pin, digest-pinned base, closure-manifest gate, Playwright fontconfig fix, and detailed `just minds-build-fct-nixos` validation path.
