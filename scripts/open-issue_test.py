import importlib.util
from pathlib import Path

import pytest

# Import the hyphenated script module by path.
_OPEN_ISSUE_PATH = Path(__file__).parent / "open-issue.py"
_spec = importlib.util.spec_from_file_location("open_issue", _OPEN_ISSUE_PATH)
assert _spec is not None and _spec.loader is not None
open_issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(open_issue)


def test_main_opens_url_with_title_and_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() builds a GitHub new-issue URL with the title and body file contents and opens it."""
    body_file = tmp_path / "body.md"
    body_file.write_text("## Bug\n\nSomething broke.")

    opened: list[str] = []
    monkeypatch.setattr(open_issue.webbrowser, "open", opened.append)

    open_issue.main(["--title", "Bug: spaces", str(body_file)])

    assert len(opened) == 1
    url = opened[0]
    assert url.startswith("https://github.com/imbue-ai/mngr/issues/new?")
    # Encoded title and body both appear in the URL.
    assert "Bug" in url
    assert "spaces" in url
    assert "Something" in url

    out = capsys.readouterr().out
    assert "Bug: spaces" in out


def test_main_errors_when_body_file_missing(tmp_path: Path) -> None:
    """main() raises when the body file does not exist."""
    missing = tmp_path / "does-not-exist.md"

    with pytest.raises(FileNotFoundError):
        open_issue.main(["--title", "x", str(missing)])
