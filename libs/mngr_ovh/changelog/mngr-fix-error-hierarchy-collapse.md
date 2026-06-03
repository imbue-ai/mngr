Collapsed redundant `except` clauses: clauses listing `VpsApiError` / `VpsProvisioningError`
alongside `MngrError` now catch just `MngrError` (those VPS errors are already `MngrError`
subclasses via `VpsDockerError`). No behavior change.
