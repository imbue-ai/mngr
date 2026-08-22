New release-level deployment test `deployment_tests/test_account_suspension.py` (minds_services tier): suspends a fresh account on a real ci env and asserts sign-in is blocked with the structured `ACCOUNT_SUSPENDED` status, the held session's state-modifying access dies within one request, the admin view reports the suspension, and unsuspend restores sign-in end to end.

The next-deploy checklist gains the suspension rollout entry: per-tier relay redeploys (connector first) for the new frps `Ping` subscription, the no-new-secrets note, and the staging tunnel-kill verification steps.
