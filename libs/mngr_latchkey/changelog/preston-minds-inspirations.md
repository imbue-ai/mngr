Both latchkey gateway spawn sites (the shared desktop gateway and the
VPS-resident gateway) now pass `--max-body-size` raised to 512 MiB (upstream
default: 10 MiB).

The gateway natively proxies GitHub's git smart-HTTP endpoints
(`/gateway/https://github.com/<owner>/<repo>.git/...`, gated by the
`github-git` scope with `github-git-read`/`github-git-write` permissions), so
agents can run `git push`/`git fetch` through it with the credential injected
server-side -- but a push's packfile scales with repo history (a minds
template push is roughly 30 MiB today), which exceeded the old 10 MiB cap and
made gateway-authenticated pushes fail. The forever-claude-template's
publish-inspiration flow now relies on this push path.
