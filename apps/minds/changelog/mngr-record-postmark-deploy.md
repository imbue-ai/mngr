- Recorded the staging (`20260930T184917Z`) and production (`20260930T203452Z`) services deploys that shipped #1409's Postmark transport in `docs/deploy/history/rollouts/postmark-transport.md`, with the Vault state checked beforehand, Danver's staging send, and the rollback.

- `next_deploy.md` now asks for a real production send through Postmark, a decision on #1480 before the next services deploy, and the ci tier's `postmark` entry push, in place of the pre-deploy Postmark item.

- The services runbook lists Postmark among the connector's dependencies and names the four shell variables a staging or production deploy must not carry.
