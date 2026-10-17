Added two entries to the workspace glossary (`apps/minds/docs/workspace/glossary.md`):

**environment** -- a single deployed instance of the minds system, owning a data root, a Modal environment, a Neon project, and a SuperTokens app; every environment belongs to exactly one tier.

**tier** -- a category of environment that determines account credentials and deploy configuration; bare-metal boxes belong exclusively to one tier, production and staging contain exactly one environment each, and the CI and Dev tiers may contain many.

Also reformatted the glossary to one sentence per line (pure reflow, no content changes), in a separate commit ahead of the additions.
