The in-place backup restore's verdict now gates on the workspace's
*restore-critical* services and on nothing else. The contract is recorded in
the new `behaviors/backup-restore/` area of the behavior corpus
(`restore-verdict.feature`); the restore-critical set currently contains only
the system interface. Full recovery reports plain success; non-critical
services still down -- including the backup service itself, which is
deliberately outside the set -- report success with a warning naming them;
a restore-critical service that does not come back (RUNNING, or cleanly
EXITED for one-shots, within the settle window) fails the operation.
Previously the restore gated on `supervisorctl restart all`'s exit code, so
any single unspawnable service -- even one that was already broken before
the restore, or one the restore neither broke nor could fix -- reported the
whole restore as FAILED even though the data restore had succeeded, while
only `host-backup` was actually verified afterwards.

Witness tests (generated via a scoped `tmr-behaviors` run against the corpus)
back-link the contract to the test suite: a new test covers the
backup-service-down example, and the full-recovery, system-interface-down,
and restore-critical-set verdict tests carry `witnesses` markers tying each
to its behavior unit, with `partial=` annotations where a single concrete
case cannot witness a Rule clause's full quantifier.
