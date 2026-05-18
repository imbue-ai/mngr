# OVH provider + pool bake/lease audit (PR #1671, area D)

Audit of the brand-new OVH classic-VPS provider stack, the OVH-side
recycle flow, the pool host bake (`mngr imbue_cloud admin pool create`)
and the connector-side lease + release endpoints.

Same verdict scheme as the deploy-safety audit: **CONFIRMED BUG**,
**DESIGN RISK** (works as coded but the design has a sharp edge),
**MINOR**, **NOT AN ISSUE** (looked suspicious, turned out fine).

## Sources

- Spec: `specs/swap-pool-to-ovh/concise.md`
- Changelogs: `mngr-ovh-pool.md`, `mngr-ovh-testing.md`, `mngr-manual_ovh.md`
- Implementation:
  - `libs/mngr_ovh/imbue/mngr_ovh/client.py` (OVH API client)
  - `libs/mngr_ovh/imbue/mngr_ovh/backend.py` (OvhProvider, the provision flow)
  - `libs/mngr_ovh/imbue/mngr_ovh/ordering.py` (cart/checkout)
  - `libs/mngr_ovh/imbue/mngr_ovh/recycle.py` (cancelled-VPS reuse)
  - `libs/mngr_ovh/imbue/mngr_ovh/iam_tags.py` (IAM v2 tag client)
  - `libs/mngr_ovh/imbue/mngr_ovh/bootstrap.py` (post-rebuild outer SSH bootstrap)
  - `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py` (`build_vps_tags`)
  - `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/cli/admin.py` (bake orchestrator)
  - `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/instance.py` (`_rewrite_container_host_name`)
  - `apps/remote_service_connector/imbue/remote_service_connector/app.py` (lease/release)
  - `apps/remote_service_connector/migrations/003_vps_address.sql`

---

## Findings

### F1. `parse_extra_tags_env` runs AFTER `order_and_wait_for_vps` — typo'd tag leaks a month of billing

**Verdict: CONFIRMED BUG (vs spec). → FIXED in commit on this branch.**

`OvhProvider._provision_vps` now calls
`parse_extra_tags_env(os.environ.get("MNGR_VPS_EXTRA_TAGS", ""))` at the
very top of the method, before `_maybe_claim_recycled_vps` and before
any state-changing API call. Pinned by source-position test
`test_f1_extra_tags_parsed_before_recycle_or_order` in
`backend_test.py` so a future refactor that moves the parse back down
breaks the test loudly.

The spec is explicit:
> "Parsing is strict: an entry without `=` is an error and aborts
> provisioning. Keys are pre-validated locally against OVH's IAM tag
> character regex so bad input fails fast, **before any API call**."

The implementation puts the parse INSIDE the post-order block:

```python
# backend.py:_provision_vps
if recycle_handle is None:
    service_name = order_and_wait_for_vps(...)       # <-- order happens (costs $)
    fresh_order_service_name = service_name
...
urn = vps_urn_for(service_name, ...)
if recycle_handle is None:
    extra_tags = parse_extra_tags_env(os.environ.get("MNGR_VPS_EXTRA_TAGS", ""))  # <-- parse happens HERE
    ...
    attach_tags(self.ovh_client, urn, all_tags)
```

If `MNGR_VPS_EXTRA_TAGS` has a typo (uppercase key, reserved key,
missing `=`, ...), `parse_extra_tags_env` raises *after*
`order_and_wait_for_vps` has succeeded. The `finally` branch then
fires `_terminate_orphaned_fresh_order` which only cancels future
renewal — the current month is already paid. The whole point of
"pre-validate locally" was to avoid this exact failure mode, and the
fix is essentially free.

**Fix:** call `parse_extra_tags_env(os.environ.get("MNGR_VPS_EXTRA_TAGS", ""))`
at the very top of `_provision_vps`, before `_maybe_claim_recycled_vps`,
and pass the parsed dict down. (The recycle path doesn't apply extra
tags, so the parsed value is only used in the fresh-order branch — but
parsing has to happen before *either* path runs since either path can
order a VPS if recycling falls through.)

---

### F2. Recycle finalize failure: VPS stays cancelled, host record points at it, user notification is just an ERROR log line

**Verdict: DESIGN RISK.**

Flow:

1. `try_recycle_cancelled_vps` locks a cancelled VPS, swaps the
   `mngr-host-id` tag to the new id, registers a `RecycleHandle` on
   the client. **Crucially does NOT flip
   `deleteAtExpiration=False`** — that's deferred.
2. `_provision_vps` rebuilds, TOFU-pins, root-bootstraps, etc.
3. mngr writes the host record to disk.
4. `_on_host_finalized(host_id, vps_ip)` is called. It calls
   `finalize_recycle(client, handle)` which:
   - Discards the handle (line 134 — **before** the API call).
   - Calls `client.set_renew_at_expiration(handle.service_name, False)`.
   - Polls for un-cancel propagation.
   - Releases the IAM lock tag.

If the un-cancel API call fails (transient OVH 5xx, network blip), the
handle is already discarded so we can't retry. `finalize_recycle`
returns `False` and logs ERROR. `_on_host_finalized` catches the
exception path but **does not check the bool return value** from the
no-exception-but-False case. The provider treats finalize as successful.

End state: host record exists, points at a VPS that will
auto-decommission at the next OVH expiration boundary. The user sees
no error during `mngr create`; their host just vanishes silently on
the OVH-side expiration date.

**Fix options:**
1. Have `finalize_recycle` raise on un-cancel failure instead of
   returning bool. Make the provider catch + re-raise (or surface as
   a non-fatal warning that's at least visible to the operator).
2. Discard the handle AFTER the un-cancel succeeds, not before, so a
   retry is possible.
3. Add a background reconciler that walks every host record and
   re-asserts `deleteAtExpiration=False` against the OVH API.

---

### F3. Two concurrent OVH orders against the same account can race: `_wait_for_new_service_name` picks `sorted(new_names)[0]`

**Verdict: DESIGN RISK (rare in practice). → FIXED + LIVE-VERIFIED on this branch.**

`order_and_wait_for_vps` no longer diffs `/vps` listings. It captures
the `orderId` from the checkout response (`order.Order`), then walks
the operations chain to find the assigned serviceName:

```
GET /me/order/{orderId}/details
    -> list of detailIds
For each detailId:
    GET /me/order/{orderId}/details/{detailId}/extension
        -> billing.ItemDetail; match on
           order.plan.code == requested_plan_code AND
           order.plan.product.name == "virtualPrivateServer"
           (filters out OS / backup / installation line items)
    GET /me/order/{orderId}/details/{detailId}/operations
        -> list of operationIds
    For each operationId:
        GET /me/order/{orderId}/details/{detailId}/operations/{opId}
            -> service.Operation; .resource.name IS the serviceName
```

**Why not the simpler `billing.OrderDetail.domain` field?** The first
F3 commit tried that, but a live probe on 2026-05-18 against the
OVH-US API revealed `domain` is always the literal `"*"` for VPS
orders -- useless for correlation. The operations chain is the only
documented path that yields the assigned serviceName.

Post-hoc verify also lands: after fetching the serviceName, the
function `GET /vps/{serviceName}` and asserts
`model.name == requested_plan` + `requested_datacenter` is a
case-insensitive substring of `zone`. Defends against any future
provider that delivers a VPS of the wrong shape.

**End-to-end live verification** (2026-05-18, one live `vps-2025-model1`
order in `US-EAST-VA`):

| Phase | Result |
|---|---|
| Order placed | orderId=7974206, cart=2305cf24-... |
| serviceName via `order_and_wait_for_vps` (operations chain) | `vps-c4aeb97e.vps.ovh.us` |
| Time-to-serviceName | ~80 seconds |
| Independent chain walk (separate script) | same `vps-c4aeb97e.vps.ovh.us` |
| Post-hoc `GET /vps/{name}` | `model.name="vps-2025-model1"`, `zone="Region OpenStack: os-us-east-va-2"` (matches) |
| Diff `/vps` before/after | exactly one new VPS: `vps-c4aeb97e.vps.ovh.us` |

The independent chain walk used detail `105339987` (plan.code=`vps-2025-model1`,
duration=`P1M`), operation `173487777`, `resource.name="vps-c4aeb97e.vps.ovh.us"`.
The order also produced 5 other details (option-linux, option-auto-backup
installation + recurring) which the plan-code filter correctly skipped.

Pinned by tests in `ordering_test.py`:
- `test_order_and_wait_for_vps_success_polled_path`
- `test_order_and_wait_for_vps_polls_when_order_detail_listing_initially_empty`
- `test_order_and_wait_for_vps_filters_out_os_subresource_detail`
- `test_order_post_hoc_verify_catches_wrong_plan`
- `test_order_post_hoc_verify_catches_wrong_region`
- `test_order_raises_when_checkout_returns_no_order_id`
- `test_order_raises_when_delivery_times_out` (exercises the new path)
- `test_f3_parallel_orders_each_get_their_own_service_name` (two threads, shared fake client, both new serviceNames visible during each thread's wait window; each thread returns its OWN serviceName)

`_wait_for_new_service_name`:

```python
current = set(client.list_instances())
new_names = current - existing_before
if new_names:
    chosen = sorted(new_names)[0]
    return chosen
```

Two `mngr create`s issued concurrently against the same OVH account
(or even from the same process via threads) both compute their own
`existing_before` snapshot. They both place orders. By the time
either polls `/vps`, both new serviceNames may be visible. Each
process picks `sorted(...)[0]` — which is **deterministic but not
necessarily theirs**.

Both processes proceed to:
- attach `mngr-provider` / `mngr-host-id` IAM tags (each overwriting
  the other's host-id since `attach_tag` is upsert)
- issue `/rebuild` against the picked serviceName
- TOFU-pin host key

The "loser" process ends up managing a VPS it didn't order — and its
own ordered VPS is being managed by the "winner." Tags get scrambled,
rebuild races itself, host records point at the wrong machines.

In practice: concurrent `mngr create` against the same OVH account is
rare (each operator usually runs one mngr at a time, and `minds pool
create --count N` is serial in its loop), but the pool-bake flow
*could* be parallelized in the future, at which point this would
silently produce wrong state.

**Fix:** correlate each order with its serviceName via the cart's
order id. `POST /order/cart/{id}/checkout` returns an order object;
the order has a `serviceId`-style reference. Use that instead of
"diff `/vps` listings." Failing that, document the serial-only
constraint loudly in `order_and_wait_for_vps`.

---

### F4. `set_renew_at_expiration` RMW sends back the entire `serviceInfos` body — clobbers concurrent OVH-dashboard edits

**Verdict: MINOR.**

`client.py:set_renew_at_expiration`:

```python
info = self.get_service_info(service_name)
renew = dict(info.get("renew") or {})
renew["deleteAtExpiration"] = delete_at_expiration
if not delete_at_expiration:
    renew["automatic"] = True
    info["renewalType"] = "automaticV2012"
info["renew"] = renew
self._call("PUT", f"/vps/{service_name}/serviceInfos", **info)
```

The docstring claims "Performs a read-modify-write on the full
`services.Service` body to avoid clobbering unrelated fields (contact
info, renewal type, etc.)" — but it actually sends the WHOLE `info`
dict back. If an admin edits `contactBilling` / `contactTech` /
`engagedUpTo` / etc. via the OVH dashboard between our GET and PUT,
we revert their change.

OVH's API may also reject `info` keys that aren't writable from the
PUT side; we'd see a 400 and the cancellation would fail. Verified-
live testing apparently passed, so either OVH accepts the full body
or only ignores read-only fields.

**Fix:** strip down the PUT body to only the fields we're mutating
(`renew.*`, `renewalType`).

---

### F5. `attach_tags` is N POSTs with no rollback — partial failure leaves the VPS with a mixed tag set

**Verdict: DESIGN RISK (small blast radius).**

`iam_tags.py:attach_tags` issues one POST per `(key, value)` pair.
There's no bulk endpoint, but there's also no rollback: if 3 of 5
tags succeed and the 4th fails, the VPS has 3 tags and is missing 2.
The `_provision_vps` `finally` branch then fires
`_terminate_orphaned_fresh_order` which cancels the VPS — so the
billing damage is bounded — but the operator never sees which tags
landed vs which didn't, and the partial tag state could confuse the
recycle path's eligibility filter if the VPS isn't actually destroyed
in time.

**Fix:** wrap the loop in a try/except that explicitly DELETEs every
already-attached tag on partial failure before re-raising. Or
document the partial-state possibility explicitly.

---

### F6. `_swap_host_id_tag` is DELETE-then-POST — brief window with no `mngr-host-id` tag

**Verdict: NOT AN ISSUE.**

`recycle.py:_swap_host_id_tag` deletes the old `mngr-host-id` tag and
then attaches the new one. If we crash between delete and post, the
VPS has no `mngr-host-id` (only `mngr-provider`). Discovery filters
on `mngr-provider`, so the VPS still surfaces. The recycle path
iterates candidates and would re-tag it on the next `mngr create`.
The brief window is harmless.

---

### F7. `_release_lock` TOCTOU between re-read and DELETE

**Verdict: NOT AN ISSUE (acknowledged in code comments).**

The docstring explicitly calls this out: "There is still a TOCTOU
window between this re-read and the DELETE (OVH IAM has no
conditional DELETE), but the worst case shrinks from 'clobber a real
lock holder' to 'delete a stale tag' / 'racing DELETE returns 404'."
Acceptable.

---

### F8. `_select_candidates` applies `max_candidates` BEFORE the eligibility filters

**Verdict: MINOR (acknowledged in config docstring).**

The cap fires on the *raw* tagged-VPS list, before the
cancellation/state/expiration filters run. With 100 mngr-tagged
active VPSes and 5 cancelled recyclable ones, you might iterate
through 10 active ones (all rejected) and miss every recyclable
candidate. Costs an extra month of billing each time you fall through
to fresh order.

`recycle_max_candidates_considered` defaults to 10 which is fine for
small accounts. As the pool grows, this would need to scale up — or
the iteration order should put cancelled VPSes first (which requires
either OVH-side filtering or a two-pass design).

---

### F9. `_wait_for_uncancel` polls for 30s with a 2s interval, but OVH propagation can take longer in practice

**Verdict: DESIGN RISK.**

`recycle.py:_wait_for_uncancel`: 30-second budget, 2-second poll. If
the propagation takes >30s, `finalize_recycle` returns False, logs
ERROR, releases the lock. Per F2, the caller doesn't notice. The VPS
might actually un-cancel a few seconds later but we've already
abandoned it.

No retry, no exponential backoff. A single transient slow propagation
silently leaves a host pointing at a VPS that will auto-decommission.

**Fix:** make the propagation timeout configurable; or instead of
polling, re-query at host-use time (e.g., as part of the host's
periodic health check) and re-issue the un-cancel if needed.

---

### F10. `_terminate_orphaned_fresh_order` is named "terminate" but actually only cancels future renewal — the current month is forfeit

**Verdict: MINOR (naming + comment).**

`backend.py:_terminate_orphaned_fresh_order` calls
`self.ovh_client.destroy_instance(...)` which calls
`set_renew_at_expiration(True)`. That stops future billing but the
already-paid month is gone.

The function's docstring says "Best-effort terminate of a
freshly-ordered OVH VPS that we are about to leak" and "requested
termination to avoid a leaked month of billing." Both phrases imply
the current month is recovered — it isn't. The current OVH product
has no instant-termination-with-refund endpoint, so this is the best
we can do, but the docstring should say so.

---

### F11. `_litellm_app_file` / `_connector_app_file` resolve via `__file__` parents[5] — fragile to module relocation

**Verdict: MINOR.**

`per_env_deploy.py:_repo_root` walks `Path(__file__).resolve().parents[5]`
to get the monorepo root. If the file is ever moved (the spec for
deploy safety mentioned moving content into a new `deploy.py`!),
the `parents[5]` count silently breaks and points somewhere else
on disk. There's a `(root / "apps").is_dir()` sanity check, so it'd
fail loudly at deploy time — but a closer-to-the-source helper
(walk up looking for `apps/`, mirroring `recover.find_monorepo_root`)
would be more robust.

Same pattern in `neon_db.pool_hosts_migrations_dir` (`parents[6]`).

---

### F12. Pool bake's chat-agent-destroy + sentinel-removal are best-effort but coupled to two FCT internals

**Verdict: DESIGN RISK.**

`admin.py:_create_single_pool_host` assumes:
- The FCT bootstrap names the initial chat agent after `host_name`
  (the bake's per-bake hex-suffixed placeholder).
- The bootstrap writes its done-sentinel to
  `/code/runtime/initial_chat_created`.

Both are FCT-side conventions; if the FCT bootstrap changes either,
the bake's cleanup silently no-ops. The chat-agent destroy is
best-effort (warns on failure) and the sentinel removal RAISES on
failure.

Failure modes if FCT drifts:
- Bake creates a "wrong-named" chat agent (named after the bake's
  hex). Bake's destroy-by-name doesn't find it; sentinel-removal
  succeeds. **First user lease creates a SECOND chat agent** (new
  name) but the old one stays in the container's mngr profile. The
  user sees two chat agents.
- Bootstrap writes the sentinel to a different path. Bake's
  rm raises `PoolBakeError`; the bake aborts mid-flight, the
  pool_hosts row is never inserted, but the VPS + Modal env are
  already provisioned. Manual cleanup needed.

**Fix:** the constants `_BAKED_SERVICES_AGENT_NAME` and
`_INITIAL_CHAT_SENTINEL_PATH` should be imported from a shared
location both the bake and the FCT bootstrap consume. Today they're
copy-pasted across two repos.

---

### F13. `_append_authorized_key` hardcodes `paramiko.Ed25519Key.from_private_key`

**Verdict: MINOR.**

`apps/remote_service_connector/.../app.py:_append_authorized_key`
line 1511:
```python
private_key = paramiko.Ed25519Key.from_private_key(io.StringIO(management_key_pem))
```

Hardcoded to Ed25519. If the operator rotates `POOL_SSH_PRIVATE_KEY`
to RSA (or anything else), the connector starts 500-ing on every
lease. This is the exact bug `mngr_ovh.bootstrap._load_private_key`
was added to fix on the OVH side. Apply the same type-agnostic
helper here.

---

### F14. `_append_authorized_key` appends without dedup; each lease accumulates one more authorized key on the VPS

**Verdict: NOT AN ISSUE.**

`authorized_keys` lines are append-only across the pool host's
lifetime. The bake installs the management key once; each lease
appends the user's key. Released hosts never get re-leased (the
admin destroy is the only path out, and it deletes the row). So
the accumulation is bounded at "one bake-install + one user-lease"
per pool host. Fine.

---

### F15. `_append_authorized_key` uses `paramiko.AutoAddPolicy()` — no host-key verification connector → VPS

**Verdict: NOT AN ISSUE in current trust model.**

The connector trusts:
- `vps_address` came from the pool_hosts DB row (operator-baked,
  trusted source).
- `POOL_SSH_PRIVATE_KEY` is the connector's own private key,
  installed at bake time on every VPS.

A MITM at this point would need to compromise OVH's routing or DNS,
which is the same trust boundary the rest of the system already
relies on. Adding strict host-key checking would require either a
known_hosts file shipped per-bake or a TOFU pin captured at bake
time and stored alongside the pool_hosts row. Worth doing eventually
but not a regression vs. existing behavior.

---

### F16. `lease_host` injects the user's SSH key but doesn't tell the user when the host's `authorized_keys` accumulates stale entries

**Verdict: NOT AN ISSUE (see F14).**

---

### F17. Bake's `_run_ssh_command` uses `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null`

**Verdict: NOT AN ISSUE in current trust model.**

`admin.py:_run_ssh_command` connects to `root@<vps_address>` with no
host-key verification:
```python
"-o", "StrictHostKeyChecking=no",
"-o", "UserKnownHostsFile=/dev/null",
```

Same trust model as F15: the VPS address was just returned by our
own OVH order, so we're trusting OVH's DNS/routing. The OVH
bootstrap pins a host key via TOFU for the in-mngr operations, but
the bake's `_run_ssh_command` runs *outside* mngr's normal session
machinery (it's installing ufw before mngr ever connects again as
root through the inner SSH layer). The dual-path is a bit smelly
but the trust assumptions are consistent.

---

### F18. `_rewrite_container_host_name` writes via SFTP with no fsync, no verify

**Verdict: MINOR.**

`mngr_imbue_cloud/instance.py:_rewrite_container_host_name` opens
the file with `sftp.open(data_json_path, "w")` and writes the
modified JSON. paramiko's `sftp.write` flushes implicitly on close
but doesn't fsync the file on the remote side. A power loss between
close and fsync could leave the file in an inconsistent state.

Also: no read-back to verify the rewrite actually landed. If SFTP
silently truncates (e.g., disk full on the container), the next FCT
bootstrap reads a broken file.

**Fix (minor):** read back the file after writing and assert
`json.loads(...) == data`. Or write to a temp path + atomic rename
on the remote side.

---

### F19. `UPDATE pool_hosts SET status='leased', leased_to_user, leased_at, host_name=%s WHERE id=%s` — no version/optimistic-lock check

**Verdict: NOT AN ISSUE.**

The previous `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` already
holds the row lock for the duration of the transaction. The UPDATE
on the same `id` within the same transaction is safe. The connector
correctly uses `with conn:` (psycopg2 commits on context exit, rolls
back on exception), so the lock is released only after both the
SELECT and UPDATE land or both abort. Good.

---

### F20. `release_host` reads `leased_to_user` first, then UPDATEs — TOCTOU between read and write

**Verdict: NOT AN ISSUE.**

The connector's `release_host` runs under a Python `with conn:` and
`conn.cursor()` (line 1804-1825). Without an explicit transaction
block (or `BEGIN`), psycopg2 still wraps the cursor calls in a
single transaction (autocommit is off by default). The SELECT + UPDATE
are in the same transaction; postgres serializable defaults would
catch a conflicting update from another process. Plus the ownership
check is on `leased_to_user` which is operator-immutable for a given
lease. Fine.

---

### F21. `pool_destroy` DELETEs the row but doesn't destroy the OVH VPS

**Verdict: NOT AN ISSUE (intentional, documented).**

The `pool_destroy` docstring explicitly says: "this does NOT destroy
the underlying OVH VPS; that is intentional so an operator can use
`mngr destroy` themselves and inspect the row state first." OK.

But: the `--force` flag drops any-status rows. If an operator
accidentally `pool destroy --force <id>` a currently-leased row, the
user's leased session keeps running (the VPS is still up, the SSH
keys still work) but the DB row is gone. Their next `mngr list` /
`mngr destroy` against the host fails with a confusing "no host
found" because the connector can't look up the host_db_id. Minor
operator footgun.

---

### F22. `vps_urn_for` defaults to `region_code="us"` — only `ovh-us` accounts work

**Verdict: NOT AN ISSUE for now.**

`iam_tags.vps_urn_for(service_name, *, region_code="us")` defaults
to "us". The OvhProvider uses `_iam_region_code(self.ovh_config.endpoint)`
which raises on non-`ovh-*` endpoints. So only `ovh-us` / `ovh-eu`
/ `ovh-ca` work, and only the explicit-derived region code is
passed. The default arg is just for tests + standalone usage. Fine.

---

### F23. Snapshot create / delete uses `_safe_get_snapshot` which 404-tolerates — but `create_snapshot` then re-fetches and raises if it's STILL None

**Verdict: NOT AN ISSUE.**

`client.py:create_snapshot` waits for the task to finish, then
re-fetches via `_safe_get_snapshot`. If the snapshot doesn't appear
after the task succeeded (Modal-level eventual consistency on
OVH's side, perhaps), the function raises MngrError. That's the
right behavior — better to fail loudly than silently claim success
without a real snapshot.

---

### F24. `wait_for_no_active_tasks` returns when `_list_active_task_ids` returns empty — TOCTOU between the empty-tasks return and the subsequent `/rebuild` call

**Verdict: NOT AN ISSUE (intentional retry strategy).**

A new task could spawn after our last poll succeeds. OVH would then
reject the `/rebuild` with the "Action not available" error. The
caller (`rebuild_vps_with_public_key`) doesn't retry, so we'd raise
and the fresh-order cleanup path fires. Costs a month of billing on
a race that's already rare. Acceptable for now.

---

### F25. Connector's `_get_pool_db_connection` opens a fresh psycopg2 connection per request — no pooling

**Verdict: NOT NEW (pre-existing behavior, out of scope for this PR).**

---

### F26. Bake's `_get_agent_info` uses `--include 'name == "..." && host.name == "..."'` query language — fragile if mngr's include syntax changes

**Verdict: MINOR.**

The bake constructs a mngr `--include` expression as a string and
relies on mngr's CLI to parse it. If mngr ever extends or revises
the include syntax (operator precedence, quoting, etc.), the
bake's hardcoded query could break in subtle ways. No clear fix
besides "test it under CI." Worth a one-liner integration test
that asserts the include query still resolves to a unique row.

---

### F27. Bake's UFW provision: `apt-get install -y ufw` could race against unattended-upgrades on the fresh VPS

**Verdict: MINOR.**

OVH's `Debian 12 - Docker` image doesn't run `unattended-upgrades`
on first boot by default (verified by inspection of the apt config),
but a future image change could enable it. dpkg lock contention
would produce a clear error and the bake would abort — not silent
corruption, just a frustrating retry loop.

---

### F28. `install_required_outer_packages` doesn't update apt sources before install

**Verdict: NOT AN ISSUE.**

The command is `apt-get update && apt-get install -y rsync`, so
sources are refreshed first. Good.

---

### F29. `_provision_vps` finally branch: if BOTH `recycle_lock_owned` AND `fresh_order_service_name` are set, both abort calls fire

**Verdict: NOT AN ISSUE (impossible state).**

The two flags are mutually exclusive: `recycle_handle is None`
implies fresh-order, `recycle_handle is not None` implies recycle.
Only one branch sets `fresh_order_service_name`; only one path makes
`recycle_lock_owned` True. The `finally` checks both flags, but at
most one can be active. OK.

---

### F30. `OvhProviderConfig.recycle_safety_margin_hours` dropped from 24 → 2

**Verdict: NOT AN ISSUE (intentional, documented).**

Spec change. Makes sense for pool workloads. Worth flagging in
operator docs as "this is more aggressive than the default 24h that
non-pool workloads might want."

---

### F31. `_iam_region_code` raises on unrecognised endpoints — including `ovh` (no suffix)

**Verdict: NOT AN ISSUE.**

The error is clear and lists the expected shapes. Good.

---

### F32. `OvhPricingMode` enum uses `.to_wire_value()` to lowercase before sending to OVH

**Verdict: NOT AN ISSUE.**

`pricing_mode.to_wire_value()` returns `self.value.lower()` because
`UpperCaseStrEnum.auto()` produces uppercase string values but
OVH's API expects lowercase. Clear and well-isolated.

---

### F33. `is_unconfigured` short-circuits discovery but NOT provisioning

**Verdict: NOT AN ISSUE (intentional).**

`OvhProvider._list_provider_vps_hostnames` returns `[]` silently
when the client is unconfigured. `_provision_vps` would fail at
the first OVH API call with a clear auth error. Reasonable
separation: discovery (read-only, common) silently no-ops; explicit
provisioning (write, rare) fails loudly.

---

### F34. `get_instance_ip` falls through to `/vps/{id}/ips` when serviceName lacks a dot — `str(ips[0])` may not produce an IP

**Verdict: MINOR (defensive code that may not work).**

`client.py:get_instance_ip`:
```python
if "." in instance_str:
    return instance_str
ips = self._call("GET", f"/vps/{instance_id}/ips")
if not ips:
    raise VpsProvisioningError(...)
return str(ips[0])
```

OVH `/ips` returns a list of *IP block records*, not bare IP strings
(e.g., `[{"ipBlock": "x.x.x.x/32"}]` or `["x.x.x.x"]` depending on
the product). `str(ips[0])` works for the bare-string case but
returns a Python dict repr for the dict case. The branch is only
reachable for "non-standard OVH product" so probably never hit, but
the defensive code is suspect.

---

### F35. Test coverage: `test_provisioning_test.py` doesn't exercise the OVH bake end-to-end

**Verdict: COVERAGE GAP.**

The spec explicitly says: "Unit tests only for this changeset; an
acceptance-level test exercising the full bake against a mocked OVH
client is **out of scope** (covered by a separate, more-realistic
testing task)."

So this is acknowledged. But the "separate, more-realistic testing
task" hasn't shipped, so the bake → lease → release flow has zero
end-to-end coverage. The only real-world validation is the manual
`dev-josh-ovh` exercise that already surfaced the 8 hotfixes called
out in `mngr-ovh-testing.md` + `mngr-manual_ovh.md`.

---

### F36. `OvhProvider._on_host_finalized` is documented to swallow exceptions to avoid failing `create_host`

**Verdict: DESIGN RISK (acknowledged).**

The docstring says "we never fail `create_host` over a billing-state
flip after the host record is already durably written. The downside
is that an unfinalized recycle leaves the VPS in its still-cancelled
state."

This is essentially F2 — the same failure mode, slightly different
framing. The decision to swallow makes operational sense (the host
exists and works for now), but it shifts a real billing failure
mode to a silent log line.

---

### F37. `OvhProvider._vps_iam_cache` invalidation is per-process, not cross-process

**Verdict: NOT AN ISSUE.**

The cache is invalidated whenever the same process tags or untags a
VPS. Cross-process consistency relies on the next process building a
fresh client + fresh cache. Acceptable for a CLI tool.

---

### F39. `set_renew_at_expiration` fails for ~minutes after a fresh order: "Unable to synchronize l1::Service, subscription is not active yet"

**Verdict: CONFIRMED BUG (discovered during live verification of F3).**

OVH's billing subsystem takes a few minutes to fully activate a
freshly-ordered VPS subscription. During that window, `PUT
/vps/{serviceName}/serviceInfos` (the cancellation flag flip) fails
with HTTP 400 `"Unable to synchronize l1::Service, subscription is
not active yet"`. Reproduced live on 2026-05-18: a `set_renew_at_expiration(True)`
call issued immediately after `order_and_wait_for_vps` returned
failed; the same call ~30 seconds later succeeded.

This affects **two real code paths**:

1. **`OvhProvider._terminate_orphaned_fresh_order`** — fired from the
   `_provision_vps` `finally` branch when the fresh-order path raises
   after `order_and_wait_for_vps` succeeded but before the host
   record is written. The finally branch calls `destroy_instance`
   which calls `set_renew_at_expiration(True)`. With OVH's billing
   delay, this call fails 400, the cleanup is logged as "manual
   cleanup may be needed", and the VPS is **fully leaked** for a
   month of billing -- the exact failure mode the cleanup was
   added to prevent (per the function's own docstring).

2. **`recycle.finalize_recycle`** (less common but symmetric) — only
   triggered if a recycled VPS's un-cancel fails, but the existing
   `set_renew_at_expiration(False)` call is on a long-lived VPS so
   it's less prone to this specific race.

The fix: `client.set_renew_at_expiration` should retry with backoff
on the specific `"subscription is not active yet"` error message
from OVH. A 30-second initial wait + retry, up to a generous cap (5
minutes), would have caught both cases. The error message is stable
in practice (it's an OVH-side billing-API standard).

**Action:** add retry-on-`"subscription is not active yet"` to
`client.set_renew_at_expiration` (and have `_terminate_orphaned_fresh_order`
log loud success after the retry completes). Without this, every
`_provision_vps` failure between fresh-order delivery and host-record
write leaks a month of billing on the just-ordered VPS.

---

### F38. `_BAKED_SERVICES_AGENT_NAME = "system-services"` is duplicated across mngr_imbue_cloud and (presumably) minds

**Verdict: MINOR (already in DEPLOY_SAFETY_AUDIT.md's spirit).**

The constant lives in `mngr_imbue_cloud/cli/admin.py` and is repeated
implicitly in minds-side code that does `mngr create system-services@<host>.imbue_cloud_<slug>`.
If the two ever drift, the user's lease results in an agent named
after the bake's id instead of "system-services" (or fails to adopt
entirely). Already covered indirectly by `--reuse` plumbing fixes in
this PR; worth a shared-constant module long-term.

---

## Summary

| Finding | Verdict | Action |
|---|---|---|
| F1: parse_extra_tags_env after order_and_wait_for_vps | **CONFIRMED BUG → FIXED** | Moved + source-position test |
| F2: recycle finalize failure silently strands the host | **DESIGN RISK** | Don't discard handle until un-cancel succeeds; or raise on finalize failure |
| F3: concurrent order race picks wrong serviceName | **DESIGN RISK → FIXED + LIVE-VERIFIED** | Operations-chain correlation + post-hoc plan/region verify + parallel-orders test; verified end-to-end against real OVH-US API |
| F39 (new): `set_renew_at_expiration` 400s for ~minutes after fresh order | **CONFIRMED BUG** | Retry on `"subscription is not active yet"`; affects `_terminate_orphaned_fresh_order` and leaks fresh-order billing |
| F4: `set_renew_at_expiration` PUTs whole serviceInfos body | **MINOR** | Send only the fields we mutate |
| F5: `attach_tags` partial-failure leaves mixed tag state | **DESIGN RISK** | Rollback on partial; or document |
| F6: `_swap_host_id_tag` brief no-host-id window | **NOT AN ISSUE** | — |
| F7: `_release_lock` TOCTOU | **NOT AN ISSUE (acknowledged)** | — |
| F8: `max_candidates` applied before filters | **MINOR (acknowledged)** | Scale up default or two-pass |
| F9: `_wait_for_uncancel` 30s budget, no retry | **DESIGN RISK** | Make configurable; or re-assert at health-check |
| F10: `_terminate_orphaned_fresh_order` misleading name | **MINOR (naming)** | Rename to `_cancel_*` + clarify docstring |
| F11: `__file__.parents[5/6]` for repo root | **MINOR** | Walk up looking for `apps/` |
| F12: bake-side chat-agent cleanup couples to FCT internals | **DESIGN RISK** | Share constants across repos |
| F13: `_append_authorized_key` hardcodes Ed25519 | **MINOR** | Apply type-agnostic loader |
| F14: authorized_keys accumulation | **NOT AN ISSUE** | — |
| F15: connector → VPS no host-key check | **NOT AN ISSUE (trust model)** | — |
| F16: user-side stale-key signal | **NOT AN ISSUE** | — |
| F17: bake `_run_ssh_command` no host-key check | **NOT AN ISSUE (trust model)** | — |
| F18: `_rewrite_container_host_name` no fsync/verify | **MINOR** | Read back + assert |
| F19: `lease_host` SELECT+UPDATE | **NOT AN ISSUE** | — |
| F20: `release_host` TOCTOU | **NOT AN ISSUE** | — |
| F21: `pool_destroy --force` on leased row | **NOT AN ISSUE (operator footgun)** | — |
| F22: `vps_urn_for` default region | **NOT AN ISSUE** | — |
| F23: snapshot create re-fetch | **NOT AN ISSUE** | — |
| F24: `wait_for_no_active_tasks` TOCTOU | **NOT AN ISSUE** | — |
| F25: connector connection-per-request | **NOT NEW** | — |
| F26: bake's mngr include query is a string | **MINOR** | Integration test |
| F27: apt-get install vs unattended-upgrades race | **MINOR** | — |
| F28: outer packages apt-get update | **NOT AN ISSUE** | — |
| F29: double-cleanup in `_provision_vps` finally | **NOT AN ISSUE** | — |
| F30: `recycle_safety_margin_hours` 24 → 2 | **NOT AN ISSUE (intentional)** | — |
| F31: `_iam_region_code` raises on unknown endpoint | **NOT AN ISSUE** | — |
| F32: `OvhPricingMode.to_wire_value` | **NOT AN ISSUE** | — |
| F33: `is_unconfigured` short-circuit | **NOT AN ISSUE** | — |
| F34: `get_instance_ip` fallback `str(ips[0])` | **MINOR (defensive code suspect)** | — |
| F35: no end-to-end bake test | **COVERAGE GAP (acknowledged in spec)** | — |
| F36: `_on_host_finalized` swallows exceptions | **DESIGN RISK (acknowledged)** | Same as F2 |
| F37: per-process IAM cache | **NOT AN ISSUE** | — |
| F38: `_BAKED_SERVICES_AGENT_NAME` constant duplicated | **MINOR** | — |

### Items I'd fix before relying on the OVH provider in production

In rough priority order:

1. ~~**F1** (parse_extra_tags_env after order)~~ — **FIXED on this branch.**
2. **F2 / F36** (recycle finalize silently strands the host) — needs a small design tweak (defer handle discard until un-cancel returns) so failed un-cancels can be retried
3. ~~**F3** (concurrent order race)~~ — **FIXED + LIVE-VERIFIED on this branch.** Operations-chain correlation + post-hoc plan/region verify + parallel-orders regression test. End-to-end verified against the real OVH-US API.
4. **F39 (new)** (`set_renew_at_expiration` 400s on freshly-ordered VPSes) — discovered during the F3 live verification. Affects `_terminate_orphaned_fresh_order`'s cleanup path -- a `_provision_vps` failure between fresh-order delivery and host-record write currently leaks a month of billing on the just-ordered VPS, because the cleanup hits the "subscription not active yet" race and gives up. Needs retry-on-`"subscription is not active yet"` in `client.set_renew_at_expiration`.
4. **F5** (attach_tags partial failure) — small fix; defends against a future intermittent IAM tag outage
5. **F9** (wait_for_uncancel 30s no retry) — same family as F2, similar fix
6. **F13** (connector hardcodes Ed25519) — one-liner, mirrors the OVH-side fix
7. **F12** (bake-FCT coupling) — needs a shared-constants module long-term
8. **F18** (`_rewrite_container_host_name` no verify) — small defense against silent SFTP truncation

Everything else is naming, performance, or coverage.

### Items deferred to the team's "separate, more-realistic testing task"

F35 (no end-to-end bake test). Many of the design-risk findings
above would surface naturally under that test framework if it
faked OVH's API responses (e.g., F2 / F9 with an injected
un-cancel failure, F3 with concurrent orders).
