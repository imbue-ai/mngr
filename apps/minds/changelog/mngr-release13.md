Minds 0.3.12.

The app version is now `0.3.12`, and a shipped binary clones the `minds-v0.3.12` default-workspace-template tag when it creates a workspace.

The launch-to-msg end-to-end test now drives the permission request the way a person does: it clicks the in-chat card's "Review & respond" button to open the review popup, then answers the request there. The test previously waited for the popup to appear on its own and timed out after 360s without ever clicking Deny, so the Slack permission flow could not pass.
