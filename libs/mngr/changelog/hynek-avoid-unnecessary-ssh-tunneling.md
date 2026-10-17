# Regenerated `mngr latchkey` command docs

`docs/commands/secondary/latchkey.md` regenerated for the new
`create-agent-env` / `forward` help text: a VPS workspace's gateway URL is now
`http://host.docker.internal:1989` (reached over the container's docker bridge),
while a desktop-gateway workspace keeps `http://127.0.0.1:1989`.

`ProviderInstanceInterface.get_container_loopback_ssh_port`'s docstring no
longer says the VPS-resident latchkey gateway reverse-tunnels into every
container: the loopback publish port only matters for a container that
predates the docker-bridge route. No behavior change.

`imbue.mngr.primitives` gains `OUTER_HOST_HOSTNAME_IN_CONTAINER`
(`host.docker.internal`, docker's conventional name for a container's host),
the one definition `mngr_vps` builds its `--add-host` mapping from and
`mngr_latchkey` builds a VPS-gateway agent's `LATCHKEY_GATEWAY` URL from.
