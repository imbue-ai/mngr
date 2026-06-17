Trimmed the README to user-relevant content (removed internal implementation details, release-test instructions, and roadmap notes) and tightened it for concision.

Aligned the GCP provider config field descriptions (surfaced via `mngr config`/help) with the README's "GCP-specific configuration" table, and corrected the `auto_shutdown_seconds` README row (the VM halts via `shutdown -P`, it does not self-delete).
