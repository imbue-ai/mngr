Region-aware host leasing.

- New migration `007_pool_host_region.sql` adds a nullable `region` column to `pool_hosts` (the OVH datacenter the pool VPS was baked in). Rows baked before this migration carry NULL and act as non-preferred fallback until rebaked.
- `POST /hosts/lease` accepts two optional fields: a hard `region` (adds an equality filter, so only hosts in that datacenter are eligible) and a soft `preferred_region` (adds an `ORDER BY` that prefers a matching region while still falling back to any available host). Both are independent of the existing JSONB attribute filter, and the lease stays a single query so the fast path is unaffected.
