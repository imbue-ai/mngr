# Getting started

## Starting the desktop client

In normal use, launch the Electron app -- either the packaged build or
`just minds-start` from this repo root for development iteration.
Electron spawns the `minds run` backend internally (default:
`http://127.0.0.1:8420`); a one-time login URL is printed to the
terminal and the system browser opens directly on that URL.

Run from source with nothing exported (`minds run`, or
`apps/minds/scripts/start-desktop.sh`), the backend targets production:
it loads the in-repo production `client.toml` and owns `~/.minds/`.
`just minds-start` always needs an env **activated in your shell** first,
because it syncs your local mngr into the workspace template and refuses
to guess whose data root that lands in -- activate `production` to get
the default, or another env to run against it:

```bash
eval "$(uv run minds-admin env activate dev-<your-user>)"   # or `staging`, `production`
just minds-start
```

Activation exports the four env vars (`MINDS_ROOT_NAME`,
`MNGR_HOST_DIR`, `MNGR_PREFIX`, `MINDS_CLIENT_CONFIG_PATH`) that
point the backend at the env's `~/.minds-<env-name>/` data root and
the env's `client.toml`. A shell that names another env via
`MINDS_ROOT_NAME` without `MINDS_CLIENT_CONFIG_PATH` (or
`--config-file`) is refused rather than silently pointed at production.

To bypass Electron and exercise the backend on its own:

```bash
minds run
```

## Creating your first agent

1. Open the login URL in your browser
2. You'll see the creation form (since no agents exist yet)
3. Fill in:
   - **Name**: a short identifier for the agent (e.g. "selene")
   - **Git repository**: URL or local path to a template repo (e.g. `https://github.com/imbue-ai/default-workspace-template`)
   - **Launch mode**: DOCKER (Docker container on this machine), LIMA (Lima VM), CLOUD (Docker on a Vultr VPS), or IMBUE_CLOUD (leased pool host via the imbue_cloud provider)
4. Click "Create" and wait for the Docker build + agent setup
5. You'll be redirected to the agent's web server when creation completes

## What happens during creation

1. The desktop client clones the repo (if URL) or uses it directly (if local path)
2. Runs `mngr create` with templates from the repo's `.mngr/settings.toml`
3. The agent starts in a tmux session with its apps and background services

Nothing sharing-related happens at create time. Publishing is
workspace-level and you start it later, from the share panel described
under [Publishing a workspace](#publishing-a-workspace).

## Accessing your agent

After creation, the agent is accessible at:
- **Local**: `https://host-{hex}.localhost:8421/` (the desktop client byte-forwards the bare workspace origin to the workspace's system interface, which serves the desktop)
- **Individual app**: `https://{app_name}.host-{hex}.localhost:8421/` (every registered service owns its own origin; nothing proxies or rewrites service traffic)
- **Shared** (while the workspace is published): `https://{label}.{host-id}.{user}.{region}.{domain}`, served over the workspace's share through the self-hosted relay. `{label}` is the service's origin label (`<service>-<rand>`, the shell's for the whole workspace). Each target in the share panel shows and copies its own link. The bare `{host-id}.{user}.{region}.{domain}` origin is deliberately not routed, and neither is a plain service-name prefix.

## Publishing a workspace

Publishing is one switch, "Enable sharing", for the whole workspace. It
lives in the workspace options, in the panel headed "Share" followed by
the workspace's name. Open that panel from the share button in the titlebar,
or from the Share button inside the workspace. Publishing gives the workspace an address on
the internet. It admits nobody by itself. The account that published
the workspace can always open it, with or without a grant.

The first publish takes 30 to 90 seconds. While the link is generated the
panel names the step under way: "Creating link", "Setting up encryption",
"Connecting to the relay", then "Verifying end to end". You can grant
permissions while the link is still being prepared.

Permissions are granted per target. The panel lists the whole workspace
first, then each app. A grant on the whole workspace applies to every app
in it, and also grants access to files, agent chats and terminal. A grant
on an app applies to that app alone. Each target has its own link, and
only people granted permission can open it.

A grant is to one person by email, or to everyone at a domain. An address
that already belongs to an Imbue account is granted to that account. An
address with no account admits whoever signs in with it. A public mail
provider cannot be granted as a domain, because that would admit anyone
who signs up there. See the [glossary](./glossary.md) for the grant
vocabulary.

A grant appears in the list the moment you add it and saves behind the
panel, so nothing locks while the write is in flight. A save that fails
marks the row "Could not save" and offers Retry. A removal takes effect as
soon as it saves, because the workspace re-reads the list on every
request.

Granting does not tell anyone. Each person's row offers Invite, which
sends them an email from Imbue naming what you shared -- the app, by the
name you read for it, or the whole workspace -- with its link, and naming
you by the verified email address on your account rather than by your
display name, so that nobody can dress an invitation up as mail from
someone else. Inviting is the one thing here that needs your own address
verified, for the same reason; everything else about sharing works
without it. The row then reads "Invited" with when, or "Could not
invite". Once they
have opened the link the row reads "Joined". A domain grant notifies
nobody: pass the link on yourself. Invite appears only while the workspace
is published and its permissions have reached Imbue Cloud, which happens
behind every save; until then the panel says the permissions have not
reached Imbue Cloud yet and sends them again on its own. Imbue limits how
many invitations an account sends in a day and how often the same person
can be invited; the panel says so when a limit refuses one. See
`specs/inviting-granted-visitors/spec.md` for the rules.

Anyone can turn these emails off for their own account on the Accounts
page, under "Email notifications". The emails an account needs, such as a
password reset, are never affected.

Turning publishing off drops the address, and anyone connected is cut
off. The list of permissions is preserved but inactive: it stays visible
and removable while off, and comes back live on the next publish with
nobody re-added. Unlinking the account from the workspace clears the list,
since nobody is then left who can answer for it.

## Environment variables and config

The remote service connector URL is taken from the per-env
`client.toml` that `minds-admin env activate` pointed `MINDS_CLIENT_CONFIG_PATH`
at (see `apps/minds/docs/deploy/reference/environments.md`). That URL hosts both the
share endpoints and the `/auth/*` routes the desktop client uses
for sign-in. Every share request authenticates with the signed-in
user's SuperTokens session, and who may access a share is controlled
by its grants document -- so no Basic-auth credentials or
`OWNER_EMAIL` need to be configured on the client. SuperTokens
credentials (API key, OAuth client secrets) live in HCP Vault (see
`apps/minds/docs/deploy/setup/vault.md`) and are pushed into Modal Secrets at
deploy time; they never need to be set on the client.

To switch envs, run `minds-admin env activate <name>` in your shell. The
activation sets `MINDS_CLIENT_CONFIG_PATH` for you -- you don't need
to pass `--config-file` manually:

```bash
# Activate a tier (staging or production):
eval "$(uv run minds-admin env activate staging)"
just minds-start

# Or a per-developer dev env:
eval "$(uv run minds-admin env activate dev-<your-user>)"
just minds-start

# Backend-only invocation (no Electron):
eval "$(uv run minds-admin env activate dev-<your-user>)"
minds run
```

To deactivate (clear the env vars from your shell):

```bash
eval "$(uv run minds-admin env deactivate)"
```

For agent-specific secrets (API keys, telegram credentials), set them in the template repo's `.env` file and ensure they're listed in `pass_env` in `.mngr/settings.toml`.
