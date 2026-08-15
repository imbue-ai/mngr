# Lost-device runbook

What to do when a device that was signed in to a minds account is lost,
stolen, or otherwise no longer trusted.

A signed-in device may hold, on disk: the account's session tokens, the DEK
file (when the account was unlocked there), materialized workspace secrets
(restic backup envs, per-host SSH client keys, host-key pins), and lease
state for any cloud workspaces it created. Revocation is therefore three
steps, in this order: cut the account's sessions, re-wrap the key bundle,
then rotate every cloud workspace's SSH material so whatever the lost device
still holds stops opening anything.

## 1. Sign out all devices

Open the hosted account page (the connector's `/manage` surface) from any
browser and use **sign out of all devices**. This revokes every SuperTokens
session for the account -- the lost device can no longer call the connector:
no record pulls, no leases, no key minting, no shares.

What this does NOT do: it does not remove anything already on the lost
device's disk. Materialized SSH keys, pins, and backup envs remain readable
there, which is why the later steps exist.

## 2. Change the master password

From a healthy, unlocked device: minds settings -> change the master
password. The change is rewrap-only -- the account DEK is unchanged; a new
wrapped bundle is pushed to the connector -- so every synced secret stays
decryptable by your remaining devices with no re-encryption sweep.

Be aware of what rewrap-only means for the lost device: if the account was
unlocked there, that device holds the raw DEK file and can still decrypt any
record blobs it cached before its sessions were cut. The password change
stops a *thief who knows the old password* from unlocking on a *new* device;
it does not claw back material the lost device already has. Treat everything
that was materialized there as exposed, and proceed to step 3.

## 3. Rotate every cloud workspace

From a healthy signed-in device that has the workspace's key material on
disk -- the machine that leased it, or any unlocked device that has synced it
(the sync materializer writes the per-host key locally):

```bash
uv run mngr imbue_cloud hosts list
uv run mngr imbue_cloud hosts rotate <host-id|host-db-id|name>   # once per cloud workspace
```

`hosts rotate` rotates everything for the slice: the per-host SSH client key
(the old key is de-authorized on both endpoints only after the new one
provably authenticates) and both endpoints' sshd host keys, pinned
user-origin in the local host-key store. minds' next sync pass detects the
changed material and re-pushes the workspace record; your other devices
converge on the rotated keys on their next pull. The lost device's copies of
the old client key stop opening the workspace the moment the rotation
completes.

## 4. Local workspaces on the lost device

Workspaces hosted *on* the lost device (docker / lima rows) are in the
thief's physical possession; there is nothing to revoke remotely. Their
synced records let you restore their data onto a new device from backups
(the record carries the restic env). If you consider the backups themselves
compromised -- the lost device holds their restic credentials too -- export
what you need, then destroy and re-create the affected workspaces so new
backup credentials are minted.

## Scope notes

- Stopped cloud workspaces: their uploaded disk artifacts are encrypted
  under operator-held keys, not device-held ones, so a lost device does not
  affect them (see the addendum in
  [security-boundaries-audit.md](./security-boundaries-audit.md)).
- An operator cannot substitute for step 3: adoption means only the user's
  devices are the pinning authority, and devices refuse a host re-keyed by
  anyone else (see "adoption" in the [glossary](./workspace/glossary.md)).
