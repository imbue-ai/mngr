The shared Sentry setup now uses a single reporting gate instead of two.

`setup_sentry` no longer takes an `is_log_inclusion_enabled` callable: the same `is_error_reporting_enabled` callable now gates both automatic error sends and whether their log/traceback attachments are collected. `submit_manual_bug_report` likewise drops its `include_logs` argument and always attaches recent logs when a logs folder is given (still a no-op when no S3 bucket is configured).
