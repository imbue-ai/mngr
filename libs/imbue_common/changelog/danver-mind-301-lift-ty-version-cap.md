MIND-301: The type-check gate, `check_no_type_errors`, fails on any ty diagnostic, warnings included, and its failure message counts and lists warnings as causes alongside errors.

`testing.py` gains `assert_model_is_frozen`, which asserts a pydantic model is declared frozen. The immutability tests use it instead of mutating a field inside `pytest.raises`, which ty 0.0.87 reports as an invalid assignment. That pydantic enforces `FrozenModel`'s config at all is tested once, in `frozen_model_test.py`.

Context managers annotate their return type as `Generator[X, None, None]` rather than `Iterator[X]`, which ty 0.0.87 deprecates for `@contextmanager`.
