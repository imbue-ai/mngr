from pathlib import Path
from textwrap import dedent

from scripts.tutorial_matcher import _block_lines_in_body
from scripts.tutorial_matcher import _extract_tutorial_block
from scripts.tutorial_matcher import find_pytest_functions
from scripts.tutorial_matcher import parse_script_blocks


def test_parse_discards_shebang_block(tmp_path: Path) -> None:
    script = tmp_path / "test.sh"
    script.write_text("#!/bin/bash\nset -euo pipefail\n\nmngr foo\n")
    blocks = parse_script_blocks(script)
    assert blocks == ["mngr foo"]


def test_parse_keeps_first_block_without_shebang(tmp_path: Path) -> None:
    script = tmp_path / "test.sh"
    script.write_text("mngr foo\n\nmngr bar\n")
    blocks = parse_script_blocks(script)
    assert blocks == ["mngr foo", "mngr bar"]


def test_parse_discards_comment_only_blocks(tmp_path: Path) -> None:
    script = tmp_path / "test.sh"
    script.write_text("# just a comment\n# another comment\n\nmngr foo\n")
    blocks = parse_script_blocks(script)
    assert blocks == ["mngr foo"]


def test_parse_keeps_blocks_with_comments_and_commands(tmp_path: Path) -> None:
    script = tmp_path / "test.sh"
    script.write_text("# do the thing\nmngr foo\n\nmngr bar\n")
    blocks = parse_script_blocks(script)
    assert blocks == ["# do the thing\nmngr foo", "mngr bar"]


def test_parse_skips_empty_blocks(tmp_path: Path) -> None:
    script = tmp_path / "test.sh"
    script.write_text("mngr foo\n\n\n\nmngr bar\n")
    blocks = parse_script_blocks(script)
    assert blocks == ["mngr foo", "mngr bar"]


def test_block_lines_match_in_indented_body() -> None:
    block = "# test foo\nmngr foo"
    section = "    # test foo\n    mngr foo"
    assert _block_lines_in_body(block, section)


def test_block_lines_do_not_match_different_body() -> None:
    block = "mngr foo"
    section = "    mngr bar"
    assert not _block_lines_in_body(block, section)


def test_extract_tutorial_block_from_docstring() -> None:
    body = dedent("""\
            \"\"\"Tutorial block:
                # test foo
                mngr foo

            Scope: it runs foo and foo happens.
            \"\"\"
            result = e2e.run("mngr foo")""")
    assert _extract_tutorial_block(body) == "    # test foo\n    mngr foo"


def test_extract_tutorial_block_from_raw_docstring() -> None:
    # Raw docstrings (r"""...""") are used when the block contains a backslash
    # (e.g. a shell \$PATH); the extractor must recognize the r prefix.
    body = 'r"""Tutorial block:\n    mngr create --cmd "echo \\$PATH"\n\nScope: prints PATH.\n"""\n    pass'
    assert _extract_tutorial_block(body) == r'    mngr create --cmd "echo \$PATH"'


def test_extract_tutorial_block_absent_returns_empty() -> None:
    body = dedent("""\
            \"\"\"Scope: a non-tutorial test with no block.\"\"\"
            result = e2e.run("mngr foo")""")
    assert _extract_tutorial_block(body) == ""


def test_extract_tutorial_block_no_docstring_returns_empty() -> None:
    body = '    result = e2e.run("mngr foo")'
    assert _extract_tutorial_block(body) == ""


def test_find_pytest_functions_discovers_test_funcs(tmp_path: Path) -> None:
    test_file = tmp_path / "test_example.py"
    test_file.write_text(
        dedent("""\
        def test_something():
            \"\"\"Tutorial block:
                mngr foo
            \"\"\"
            pass

        def helper():
            pass

        def test_other():
            pass
        """)
    )
    funcs = find_pytest_functions(tmp_path)
    names = [sig.split("(")[0] for sig, _, _ in funcs]
    assert names == ["def test_something", "def test_other"]


def test_find_pytest_functions_returns_tutorial_block(tmp_path: Path) -> None:
    test_file = tmp_path / "test_example.py"
    test_file.write_text(
        dedent("""\
        def test_with_block():
            \"\"\"Tutorial block:
                mngr foo
            \"\"\"
            pass

        def test_no_block():
            \"\"\"Scope: no tutorial block here.\"\"\"
            pass
        """)
    )
    funcs = find_pytest_functions(tmp_path)
    assert "mngr foo" in funcs[0][1]
    assert "mngr foo" not in funcs[1][1]


def test_find_pytest_functions_recurses_subdirs(tmp_path: Path) -> None:
    subdir = tmp_path / "sub"
    subdir.mkdir()
    test_file = subdir / "test_nested.py"
    test_file.write_text("def test_nested():\n    pass\n")
    funcs = find_pytest_functions(tmp_path)
    assert len(funcs) == 1
    assert "test_nested" in funcs[0][0]
