Failed workspace SSH and start/stop requests now lead with what actually went wrong.

Both reported `mngr`'s output in the order mngr emits it, which puts provider-level discovery warnings first -- and those routinely name unrelated hosts. An agent asking to reach another workspace was told "outer SSH unreachable for host `<some long-destroyed workspace>`" and reasonably concluded the target was down, when the real reason came later, or (for SSH) was not in stderr at all.

The verdict now comes first and the rest of mngr's output is kept behind it: the warnings are real diagnostics, and the caller is an agent on another host that cannot read this one's logs, so dropping them would leave it no copy at all.

- `POST /api/v1/workspaces/<id>/ssh` takes the per-agent reason from `mngr exec`'s JSON report -- where it lives, and where stderr alone never mentions it -- then appends stderr as labelled context, capped.

- `POST /api/v1/workspaces/<id>/start` and `/stop` include the reason rather than only "Could not start the workspace host", with mngr's `ERROR:` lines promoted ahead of the warnings that precede them.

- Both log mngr's complete output regardless, so the untruncated copy survives locally.
