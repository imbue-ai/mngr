import pytest

from imbue.skitwright.data_types import CommandResult
from imbue.skitwright.expect import expect


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(command="test-cmd", exit_code=0, stdout=stdout, stderr=stderr)


def _fail(exit_code: int = 1, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(command="test-cmd", exit_code=exit_code, stdout=stdout, stderr=stderr)


# --- ResultExpectation ---


def test_to_succeed_passes_on_zero_exit() -> None:
    expect(_ok()).to_succeed()


def test_to_succeed_fails_on_nonzero_exit() -> None:
    with pytest.raises(AssertionError, match="exit code 1"):
        expect(_fail()).to_succeed()


def test_to_fail_passes_on_nonzero_exit() -> None:
    expect(_fail()).to_fail()


def test_to_fail_fails_on_zero_exit() -> None:
    with pytest.raises(AssertionError, match="Expected command to fail"):
        expect(_ok()).to_fail()


def test_to_have_exit_code_passes_on_match() -> None:
    expect(_fail(exit_code=42)).to_have_exit_code(42)


def test_to_have_exit_code_fails_on_mismatch() -> None:
    with pytest.raises(AssertionError, match="Expected exit code 42 but got 1"):
        expect(_fail()).to_have_exit_code(42)


def test_to_succeed_error_includes_context() -> None:
    result = _fail(stdout="some output", stderr="some error")
    with pytest.raises(AssertionError, match="some output") as exc_info:
        expect(result).to_succeed()
    assert "some error" in str(exc_info.value)
    assert "test-cmd" in str(exc_info.value)


# --- StringExpectation ---


def test_to_contain_passes_on_substring() -> None:
    expect("hello world").to_contain("world")


def test_to_contain_fails_on_missing() -> None:
    with pytest.raises(AssertionError, match="to contain"):
        expect("hello").to_contain("world")


def test_not_to_contain_passes_on_missing() -> None:
    expect("hello").not_to_contain("world")


def test_not_to_contain_fails_on_present() -> None:
    with pytest.raises(AssertionError, match="not to contain"):
        expect("hello world").not_to_contain("world")


def test_to_match_passes_on_regex_match() -> None:
    expect("version 1.2.3").to_match(r"\d+\.\d+\.\d+")


def test_to_match_fails_on_no_match() -> None:
    with pytest.raises(AssertionError, match="to match pattern"):
        expect("no numbers").to_match(r"\d+")


def test_not_to_match_passes_on_no_match() -> None:
    expect("no numbers").not_to_match(r"\d+")


def test_not_to_match_fails_on_match() -> None:
    with pytest.raises(AssertionError, match="not to match pattern"):
        expect("abc123").not_to_match(r"\d+")


def test_to_equal_passes_on_exact_match() -> None:
    expect("exact").to_equal("exact")


def test_to_equal_fails_on_mismatch() -> None:
    with pytest.raises(AssertionError, match="to equal"):
        expect("actual").to_equal("expected")


def test_to_be_empty_passes_on_empty_string() -> None:
    expect("").to_be_empty()


def test_to_be_empty_fails_on_nonempty() -> None:
    with pytest.raises(AssertionError, match="to be empty"):
        expect("not empty").to_be_empty()


# --- expect() dispatch ---


def test_expect_dispatches_to_result_expectation() -> None:
    expectation = expect(_ok())
    expectation.to_succeed()


def test_expect_dispatches_to_string_expectation() -> None:
    expectation = expect("hello")
    expectation.to_contain("hello")


def test_expect_raises_on_unsupported_type() -> None:
    with pytest.raises(TypeError, match="does not support"):
        expect(42)  # type: ignore[call-overload]
