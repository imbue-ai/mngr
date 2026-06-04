Renamed the in-memory Modal test doubles from the `Testing*` prefix to the
repo-standard `Fake*` prefix: `FakeModalInterface`, `FakeApp`, `FakeSandbox`,
`FakeVolume`, `FakeSecret`, `FakeImage`, `FakeFunction`, `FakeExecProcess`,
`FakeExecOutput`. This matches the dominant `Fake*` convention for test doubles
across the codebase, accurately describes them (working in-memory/local
implementations, not mocks/stubs), and stops pytest from trying to collect them
as test classes (the old `Testing*` names matched `python_classes = Test*`,
which produced "cannot collect test class" warnings). No behavior change.
