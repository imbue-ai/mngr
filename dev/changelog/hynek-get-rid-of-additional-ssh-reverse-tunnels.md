# Repo-root spec annotation

[`specs/minds-rest-api/spec.md`](../../specs/minds-rest-api/spec.md)
got a top-of-file banner noting that the per-agent `MINDS_API_KEY` and
the per-agent reverse SSH tunnel for the Minds API are both gone --
agents now reach the API exclusively through the latchkey gateway's
`minds-api-proxy` extension, with a single installation-wide
`MINDS_API_KEY`. See the changelogs for the `minds` and `mngr_latchkey`
projects for the full design + implementation notes.
