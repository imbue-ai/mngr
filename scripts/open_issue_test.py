from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest

from scripts import open_issue


def test_main_opens_url_with_title_and_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() builds a GitHub new-issue URL routing the title and body file contents into the
    correct query parameters, then hands it to the injected opener."""
    body_text = "## Bug\n\nSomething broke."
    body_file = tmp_path / "body.md"
    body_file.write_text(body_text)

    opened: list[str] = []
    open_issue.main(["--title", "Bug: spaces", str(body_file)], open_url=opened.append)

    assert len(opened) == 1
    url = opened[0]
    split = urlsplit(url)
    assert f"{split.scheme}://{split.netloc}{split.path}" == "https://github.com/imbue-ai/mngr/issues/new"
    # The title and the body file's contents must land in their own query params (a
    # title/body swap or a missing-encoding regression would fail these exact-match checks).
    query = parse_qs(split.query)
    assert query["title"] == ["Bug: spaces"]
    assert query["body"] == [body_text]

    out = capsys.readouterr().out
    assert "Bug: spaces" in out


def test_main_errors_when_body_file_missing(tmp_path: Path) -> None:
    """main() raises when the body file does not exist."""
    missing = tmp_path / "does-not-exist.md"

    with pytest.raises(FileNotFoundError):
        open_issue.main(["--title", "x", str(missing)])
