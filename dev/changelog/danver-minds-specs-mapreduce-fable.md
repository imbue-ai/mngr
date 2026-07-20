Add the `tmr-specs-minds` justfile recipe: the canonical invocation of the new `mngr tmr-specs` recipe for the minds behavioral-spec corpus (`--root apps/minds/specs --name tmr-specs-minds --mapper-prompt apps/minds/tmr/specs_mapper.j2`, no testing flags after `--` since spec mappers run their touched witness tests by node id).

Register `tmr-specs` in `scripts/make_cli_docs.py` so its docs page is generated.

Add the blueprint for the feature at `blueprint/tmr-specs/plan-tmr-specs.md` (de-complected during design; the simplification memo leads the file).
