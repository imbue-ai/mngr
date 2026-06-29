Accelerated imbue_cloud bare-metal slice bakes by building the forever-claude-template (FCT) image once per box instead of once per slice.

The first production (`--from-tag`) slice baked on a box builds the FCT image, bakes Playwright/Chromium into it, and saves it to a box-local tar; every subsequent slice on that box `docker load`s that tar instead of rebuilding from the Dockerfile. This removes the per-slice 10-20 minute image build and the per-slice ~900s first-boot Playwright install for all but the first slice.

The image is transferred entirely over the box's own loopback (no external bandwidth, nothing left reachable from a leased slice), using a unique ephemeral SSH key per transfer that is destroyed afterward. Dev (`--workspace-dir`) bakes are unchanged and always build from the Dockerfile.
