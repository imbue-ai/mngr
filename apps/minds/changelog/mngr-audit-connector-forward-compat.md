Made the desktop client forward compatible with newer imbue cloud deployments, per the connector-forward-compat plan.

Workspace records carry a `record_format`: a record written by a newer app version stays visible (and its workspace connectable), but pushes, destroy/tombstone, disassociate, and removal are refused with "update the app to manage this machine" (the destroy API refuses up front with a 409). When a pull finds the server row at a newer format than a locally-dirty copy, the server row wins (the pending local change could never be pushed) and the record becomes read-only here. Encrypted secrets blobs likewise carry a `payload_format`: a blob written by a newer version is never rewritten, and rewrites round-trip unknown payload keys verbatim so newer clients' material is never dropped.

The CLI-output models the desktop parses from the bundled imbue_cloud plugin now use the plugin's tolerant `WireModel` base, and the connector's structured "client too old" (HTTP 426) refusal surfaces as a typed error with the update message.

`minds env deploy` now bakes the deploy id into both connector frontend bundles (their `X-Imbue-Client` build stamp) and the release process gains a wire-compat snapshot corpus step (see docs/release.md).

New `remote-compatibility` behaviors corpus folder specifies the forward-compatibility contract, with witnessing tests.
