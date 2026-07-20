Added the `behavioral-specs` skill (`.claude/skills/behavioral-specs/SKILL.md`): the definitional reference for the behavioral-spec language - Gherkin `.feature` files in per-project corpora at `<project>/specs/` (the minds corpus at `apps/minds/specs/` is the first), validity via `gherkin-official`, kebab-case folder/file/tag naming, first-tag identity with folder-derived coordinates unique per corpus, invariants as `Rule:` blocks with folder scoping bounded by the corpus (reserved `invariants.feature` and `overview.md` basenames), the `witnesses` test back-link convention with its corpus/test-tree pairing rule, and the `mngr specs` CLI.

Added the blueprint plan `blueprint/minds-behavioral-specs/` covering the skill, the CLI, and the re-expression of the authentication proof-of-concept spec in the new language.

Root `pyproject.toml` gains the `imbue.mngr_specs` coverage flag required by the meta-ratchet, and `scripts/make_cli_docs.py` adds `specs` to the generated secondary-command docs set.

Updated the root `uv.lock` for the new `libs/mngr_specs` library (which carries the `gherkin-official` dependency).
