Workspace-sync SSH semantics move onto the host-key pin store (user-controlled keys, Phase 4).

Producers now render a record's `ssh_known_hosts` from the store's clean current pins (one per endpoint and keytype) instead of slurping the known_hosts file, so stale or duplicated lines can no longer enter a synced record; the broken lima provider-wide key fallback is gone, and provider-wide keys can never enter a record.

Importers parse the synced `ssh_known_hosts` into pins and apply them through the store as user-origin material (replace per endpoint and keytype), gated on the record's revision via a new local-only `last_applied_secrets_revision` replica field: a record this device already applied -- or an older one -- can neither clobber nor re-stamp newer local material (e.g. a key rotation run on this machine), and the synced private key is only (re)written under the same gate. The leasing device's lease.json skip is unchanged.

A successful materialization now also stamps the local secrets parity digest, so a key rotation (`mngr imbue_cloud hosts rotate`) run from any device that synced the workspace -- not just the machine that leased it -- is re-pushed by the next sync pass and propagates to the other devices.

The backup env converges under the same gate, for every provider kind: a device holding a restic env that differs from the record's picks up the record's version on its next sync pass (previously a drifted local env was kept forever, so backup status/export there could silently use outdated credentials). A locally-newer env is protected exactly like a local key rotation -- the producer's parity stamp keeps the gate closed until its own change is pushed -- and a payload without an env never deletes a local one.

The standalone RSA -> Ed25519 client-key migration (the background scheduler and the `minds migrate-ssh-keys` CLI, both unreleased) is removed: slice adoption in the imbue_cloud plugin now rotates legacy RSA client keys to Ed25519 through the reconciler desired state, which survives VM restarts (the standalone migration's raw `authorized_keys` append was reverted by cidata replay, and by the reconciler on adopted hosts) and de-authorizes the retired RSA key. `mngr imbue_cloud hosts rotate` is the manual path.

Docs: new lost-device runbook (`docs/lost-device-runbook.md`), an "adoption" glossary entry, and a security-boundaries-audit addendum covering the adoption trust-model change and the operator-decryptability of stopped-workspace artifacts.
