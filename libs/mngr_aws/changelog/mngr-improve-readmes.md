Trimmed the README to user-relevant content (removed internal implementation details, release-test instructions, and roadmap notes) and tightened it for concision.

Aligned the `AwsProviderConfig` field descriptions (surfaced via `mngr config` / help) with the README configuration table so the two are consistent.

Fact-checked the README against the collapsed `default_ami_id` (a single nullable field that falls back to the pinned per-region default) and documented the required offline-state S3 bucket that `mngr aws prepare` creates.
