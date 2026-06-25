from imbue.imbue_common.pure import pure


def test_pure_decorator_returns_the_same_function_object() -> None:
    def add(a: int, b: int) -> int:
        return a + b

    # pure is an advisory no-op decorator: it must return the original function unchanged.
    assert pure(add) is add


def test_pure_decorator_preserves_function_name() -> None:
    @pure
    def my_function() -> str:
        return "hello"

    assert my_function.__name__ == "my_function"
