from imbue.minds.desktop_client.workspace_version import parse_git_describe
from imbue.minds.desktop_client.workspace_version import parse_upgrade_merges


def test_parse_git_describe_returns_tag() -> None:
    assert parse_git_describe("minds-v0.3.3\n") == "minds-v0.3.3"


def test_parse_git_describe_returns_none_when_empty() -> None:
    assert parse_git_describe("") is None
    assert parse_git_describe("   \n") is None


def test_parse_upgrade_merges_parses_tab_separated_lines() -> None:
    stdout = (
        "aaaa1111\t2026-06-01T12:00:00+00:00\tupgrade attempt 2: minds-v0.3.2 -> minds-v0.3.3\n"
        "bbbb2222\t2026-05-01T09:30:00+00:00\tupgrade attempt 1: minds-v0.3.1 -> minds-v0.3.2\n"
    )

    merges = parse_upgrade_merges(stdout)

    assert len(merges) == 2
    assert merges[0].commit_sha == "aaaa1111"
    assert merges[0].summary == "upgrade attempt 2: minds-v0.3.2 -> minds-v0.3.3"
    assert merges[0].committed_at is not None
    assert merges[0].committed_at.tzinfo is not None
    assert merges[1].commit_sha == "bbbb2222"


def test_parse_upgrade_merges_tolerates_empty_subject_and_unparseable_time() -> None:
    stdout = "cccc3333\tnot-a-time\t\n"

    merges = parse_upgrade_merges(stdout)

    assert len(merges) == 1
    assert merges[0].commit_sha == "cccc3333"
    assert merges[0].summary == ""
    assert merges[0].committed_at is None


def test_parse_upgrade_merges_skips_blank_and_malformed_lines() -> None:
    stdout = "\n  \nonlyonefield\ndddd4444\t2026-06-01T12:00:00Z\tmerged\n"

    merges = parse_upgrade_merges(stdout)

    assert len(merges) == 1
    assert merges[0].commit_sha == "dddd4444"


def test_parse_upgrade_merges_handles_tabs_in_subject() -> None:
    # The subject is the third field; an embedded tab in the message must not
    # split it (split has maxsplit=2).
    stdout = "eeee5555\t2026-06-01T12:00:00Z\tmerged\twith\ttabs\n"

    merges = parse_upgrade_merges(stdout)

    assert len(merges) == 1
    assert merges[0].summary == "merged\twith\ttabs"


def test_parse_upgrade_merges_empty_output_is_empty_tuple() -> None:
    assert parse_upgrade_merges("") == ()
