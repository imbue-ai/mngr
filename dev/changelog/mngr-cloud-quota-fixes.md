Raised the code-review stop hook's CI poll timeout to 900 seconds (`.reviewer/settings.json`). The default 600-second window was shorter than the median CI run (~620s on main and feature branches alike), so the hook's timeout path -- which reports with the same "CI tests have failed" message as a real failure -- fired on the majority of green runs.

Added the `blueprint/cloud-quota-fixes/` plan for the R2 storage-quota fixes (sweep query rework, confirmed downgrades, cleanup grants, creation-time storage gate).
