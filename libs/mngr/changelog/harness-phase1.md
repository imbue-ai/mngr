- A create whose readiness wait times out now destroys the half-started agent before reporting failure, so a failed create leaves no zombie behind.

- The destroy-time orphan-process sweep uses a single grep over /proc/*/environ instead of one grep fork per process, cutting the scan from ~1-2s (and past its 10s bound under parallel multi-agent destroys, where it silently skipped kills) to under 0.1s.
