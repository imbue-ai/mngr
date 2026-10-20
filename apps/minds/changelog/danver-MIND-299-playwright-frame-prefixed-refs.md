Lifted the `<1.60` cap on playwright, so the app resolves 1.62.0 again.

The cap was added because 1.62 prefixes an aria snapshot's element refs with the ordinal of the frame the element sits in -- `[ref=f3e9]` where 1.59 printed `[ref=e9]` -- which the UI-flow step script read as the whole page having changed. That script now accepts the prefixed form, so the cap has done its job; see apps/minds_evals' entry for what had to learn the new shape.

Nothing in this app changes behaviour. It declares `playwright>=1.40.0` as it did before the cap, and the box installs whatever the root lock resolves.
