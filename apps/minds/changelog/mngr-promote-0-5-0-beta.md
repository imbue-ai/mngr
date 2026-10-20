The beta channel now serves minds 0.5.0 (build `260902shwco3ynx`) to 50% of installs, up from 0.4.2 (`260825un55i8ix7`) at 100%. The remaining half stays on 0.4.2 until the rollout widens. Alpha has been on this build since 2026-09-03; stable stays on 0.4.2.

`_DEFAULT_TARGET_BY_PLATFORM` in `accounts_web.py` is deliberately unchanged. It backs the public download link while the feed is unreadable, and must never lead stable, since `allowDowngrade` is false.
