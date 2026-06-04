Bumped GitHub Actions that were pinned to Node.js-20 runtimes (deprecated by
GitHub; forced to Node 24 starting 2026-06-16) to their latest Node.js-24
majors: `actions/cache` v4->v5, `actions/upload-artifact` v4->v7,
`actions/setup-node` v4->v6, `actions/checkout` v4->v6 (vet.yml),
`extractions/setup-just` v2->v4, `mikepenz/action-junit-report` v5->v6, and
`astral-sh/setup-uv` v6->v7. This removes the Node.js-20 deprecation warnings
from CI logs.

Upgraded two vulnerable transitive dependencies in `uv.lock` to their fixed
versions (surfaced by `uv audit`): `idna` 3.14->3.16 and `starlette`
1.0.0->1.0.1.
