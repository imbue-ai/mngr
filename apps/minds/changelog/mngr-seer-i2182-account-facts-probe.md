When a skill chat (such as a scheduled `/update-self` run) falls back to a bare `mngr create` and the workspace refuses it, the error log now also records the workspace's account store and template version:

- whether the chat app's account index exists;

- how many accounts it lists, and how many of those still have their folder on disk;

- the template version its git names: the version its last `update-self` run recorded, else its nearest `minds-v*` release tag (read without fetching).

These facts separate a workspace that has no usable account (so the refusal is correct) from one that has an account but never wrote its `.mngr/settings.local.toml`, which would be a bug in the template's settings writer. Only counts and the version are recorded, never an account's id, name or provider.
