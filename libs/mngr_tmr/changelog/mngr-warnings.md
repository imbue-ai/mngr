Marked the `TestResult` and `TestMapReduceResult` result models with
`__test__ = False` so pytest no longer attempts to collect them as test
classes (their names start with "Test"). This silences the "cannot collect
test class ... because it has a __init__ constructor" warnings in CI. No
behavior change.
