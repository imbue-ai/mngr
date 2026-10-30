Sized each `mngr schedule add` release test's command budget from the per-test
timeout it has to fit inside.

Each of these tests gave its deploy command the same timeout as the test's own
`pytest.mark.timeout`, then cleaned its Modal app up inside that same budget.
The command's timeout could never be reached: pytest-timeout always fired
first, reporting a bare test timeout rather than which command was stuck and
what it had printed. Two of the four also had a lower ceiling than their
siblings, and that killed `test_schedule_add_with_verification` at exactly its
limit in the 2026-10-28 release run. The ceiling is now one value for all four,
and each command gets what it leaves.
