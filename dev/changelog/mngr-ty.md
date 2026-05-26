# Bump `ty` to 0.0.39, plus paramiko/coolname dependency bumps

- Raised the `ty` type checker floor from `0.0.24` to `0.0.39` (root `pyproject.toml`).
- Bumped pinned dependencies in `uv.lock`: `paramiko` 3.5.1 -> 4.0.0 and `coolname` 3.0.0 -> 5.0.0. The paramiko bump also pulls `pyinfra` 3.6.1 -> 3.8.0 and adds `invoke` and `types-paramiko` transitively (pyinfra 3.8.0 depends on `types-paramiko`).
  - Note: paramiko 4.0.0 is the ceiling while we depend on `pyinfra`; pyinfra 3.8.0 constrains `paramiko<5`, so paramiko 5.0.0 is not yet installable.
  - The newly-present `types-paramiko` stubs make ty type-check paramiko usage for the first time; resulting type errors were fixed across the affected projects.
- Behavioral note for contributors: `ty` 0.0.39 no longer honors the bracketed PEP-484 form `# type: ignore[<mypy-code>]`. Only bare `# type: ignore` and `ty`'s own `# ty: ignore[<ty-rule>]` are respected. All bracketed `# type: ignore[...]` comments in the repo were converted to `# ty: ignore[...]` using ty's rule names.
- Documented in `CLAUDE.md` (the "# Ratchets" section) how to tighten a ratchet count after reducing violations: `uv run pytest --inline-snapshot=trim <test_ratchets.py>` (only `=trim` lowers a count that already passes its `<=` check; `=fix`/`=update` do not).
- Tightened recorded ratchet violation counts to their current exact values across all projects via `--inline-snapshot=trim`, locking in previously-unrecorded reductions (test-config only; no source or behavior change).
