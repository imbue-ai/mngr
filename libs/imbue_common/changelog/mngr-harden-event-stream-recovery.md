The JSONL log records written through `imbue_common` logging now include the formatted traceback of a logged exception, in a new `exception.traceback_text` field, capped at 8,000 characters (keeping the innermost frames). Previously they recorded only the exception's type and message plus a `traceback` boolean, so a log file alone could not show where an unexpected exception was raised. The `traceback` boolean is unchanged.

`format_exception_traceback`, the capped formatter this uses, moved here from mngr (`imbue.mngr.utils.error_utils`) as `imbue.imbue_common.tracebacks`.
