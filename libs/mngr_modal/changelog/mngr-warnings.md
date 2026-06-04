Updated references to the renamed `modal_proxy` test doubles: `TestingModalInterface`
is now `FakeModalInterface` (and the rest of the `Testing*` Modal family is now
`Fake*`). Affects the `make_testing_provider`/`testing_modal` test helpers and
fixtures, which now reference `FakeModalInterface`. No behavior change.
