"""Release test: exporting a restored tree holding a file over the 2 GiB zip limit."""

import zipfile
from pathlib import Path

import pytest

from imbue.minds.desktop_client.backup_export import _zip_directory_contents


@pytest.mark.release
@pytest.mark.timeout(300)
def test_zip_directory_contents_handles_file_over_zip64_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    large_file = source / "large.bin"
    large_size = zipfile.ZIP64_LIMIT + 1
    # Sparse, so the source costs no disk; the zip of zeros stays a few MB.
    with large_file.open("wb") as f:
        f.truncate(large_size)
    zip_path = tmp_path / "out.zip"

    _zip_directory_contents(source, zip_path)

    with zipfile.ZipFile(zip_path) as archive:
        assert archive.getinfo("large.bin").file_size == large_size
        assert archive.testzip() is None
