Strengthened several weak tests in the remote_service_connector test suite:

- `FakeCloudflareOps` now persists created Access service tokens so the create/list/delete
  round-trip is exercised for real; the service-token round-trip tests now assert the created
  token is surfaced by the listing (and that the client secret is omitted on listings) instead
  of asserting an always-empty list.
- Migration schema-drift guards now match column names on word boundaries, against
  comment-stripped SQL, so a dropped column fails the test even when the column name still
  appears in the migration's header comment prose (e.g. `is_paid = true`) or as a substring of
  another identifier (e.g. the bare `access` column inside `access_key_id`).
- Split the mislabeled `make_tunnel_name` truncation test into a prefix-strip test plus a new
  test that actually exercises truncation to 16 characters.
- The KV "create namespace when missing" test now asserts the namespace create POST fired
  exactly once.
- Documented the intentional "returns without raising" contract for two no-positive-observable
  tests.
- Moved the `TestClient`-driven end-to-end tests (routes, auth, host-lease, paid-list CRUD,
  bucket endpoints) out of the unit `app_test.py` into a new integration file
  `test_remote_service_connector_routes.py`, leaving the pure-function and single-layer unit
  tests in `app_test.py`. The two cleanup-sweep tests now build their fake backend directly
  instead of through the route test client.

Test-only changes; no user-visible behavior change.
