"""Example usage of the ratchets module to find TODOs in Python files."""

from pathlib import Path

from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import _resolve_blame_dates
from imbue.imbue_common.ratchet_testing.core import get_ratchet_failures


def main() -> None:
    # Find all TODO comments in Python files in the current directory
    folder_path = Path.cwd()
    extension = FileExtension(".py")
    pattern = RegexPattern(r"# TODO:.*")

    chunks = get_ratchet_failures(folder_path, extension, pattern)

    # Resolve blame dates (needed to display last-modified info)
    dated_chunks = _resolve_blame_dates(chunks)

    # Print results
    for dated in dated_chunks:
        print(f"\n{dated.chunk.file_path}:{dated.chunk.start_line}")
        print(f"  Last modified: {dated.last_modified_date}")
        print(f"  Content: {dated.chunk.matched_content}")


if __name__ == "__main__":
    main()
