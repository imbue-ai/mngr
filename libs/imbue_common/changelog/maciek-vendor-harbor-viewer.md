- Ratchet scans skip binary files instead of dying on them. A scan with no extension filter reaches
  every non-ignored file in a project, so a project that vendors a frontend -- tracking fonts and
  icons beside its source -- would fail the whole ratchet suite with a `UnicodeDecodeError` rather
  than report its violations. A file that is not text cannot contain a text pattern, so excluding
  it costs no coverage.

- Ratchet scans exclude binary files in two passes, and read undecodable bytes as replacement
  characters instead of raising. `BINARY_FILE_EXCLUSION` (now covering font formats too) is applied
  centrally before any file is opened, so every scan gets it rather than only the two that
  remembered to pass it; a NUL-byte sniff of what remains catches the formats the list does not
  name, which is what a vendored frontend's assets would otherwise have taken the scan down with.
